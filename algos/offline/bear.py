import copy
import os
import numpy as np
import torch
import torch.nn.functional as F
from common.buffers import OfflineBuffer
from utils.train_tools import soft_target_update, evaluate
from utils import log_tools
from common.networks import MLPSquashedReparamGaussianPolicy, CVAE, MLPQsaNet


class BEAR_Agent:
    """
    Implementation of Bootstrapping Error Accumulation Reduction (BEAR)
    https://arxiv.org/abs/1906.00949
    This is BEAR based on SAC, which is suitable for continuous action space.
    BEAR's MMD Loss's weight alpha is tuned automatically by default.

    Actor Loss: alpha_prime * (MMD Loss - threshold) + SAC Loss
    Critic Loss: Like BCQ
    Alpha Loss: Same as SAC
    Alpha_prime Loss: -alpha_prime * (MMD Loss - threshold)

    """
    def __init__(self,
                 env,
                 data_buffer: OfflineBuffer,
                 policy_net: MLPSquashedReparamGaussianPolicy,  # actor
                 q_net1: MLPQsaNet,  # critic
                 q_net2: MLPQsaNet,
                 cvae_net: CVAE,
                 policy_lr=1e-4,
                 qf_lr=3e-4,
                 cvae_lr=3e-4,
                 gamma=0.99,
                 tau=0.05,
                 alpha=0.5,
                 auto_alpha_tuning=False,

                 # BEAR
                 lmbda=0.75,  # used for double clipped double q-learning
                 mmd_sigma=20.0,  # the sigma used in mmd kernel
                 kernel_type='gaussian',  # the type of mmd kernel(gaussian or laplacian)
                 lagrange_thresh=0.05,  # the hyper-parameter used in automatic tuning alpha in cql loss
                 n_action_samples=100,  # the number of action samples to compute the best action when choose action
                 n_target_samples=10,  # the number of action samples to compute BCQ-like target value
                 n_mmd_action_samples=4,  # the number of action samples to compute MMD.
                 warmup_step=40000,  # do support matching with a warm start before policy(actor) train

                 max_train_step=1000000,
                 log_interval=1000,
                 eval_freq=5000,
                 train_id="bear_hopper-medium-v2_test",
                 resume=False,  # if True, train from last checkpoint
                 device='cpu',
                 ):

        self.env = env
        self.data_buffer = data_buffer

        self.device = torch.device(device)

        # the network and optimizers
        self.policy_net = policy_net.to(self.device)
        self.q_net1 = q_net1.to(self.device)
        self.q_net2 = q_net2.to(self.device)
        self.target_q_net1 = copy.deepcopy(self.q_net1).to(self.device)
        self.target_q_net2 = copy.deepcopy(self.q_net2).to(self.device)
        self.cvae_net = cvae_net.to(self.device)
        self.policy_optimizer = torch.optim.Adam(self.policy_net.parameters(), lr=policy_lr)
        self.q_optimizer1 = torch.optim.Adam(self.q_net1.parameters(), lr=qf_lr)
        self.q_optimizer2 = torch.optim.Adam(self.q_net2.parameters(), lr=qf_lr)
        self.cvae_optimizer = torch.optim.Adam(self.cvae_net.parameters(), lr=cvae_lr)

        self.gamma = gamma
        self.tau = tau
        self.alpha = alpha  # SAC's temperature alpha
        self.auto_alpha_tuning = auto_alpha_tuning

        if self.auto_alpha_tuning:
            self.target_entropy = -np.prod(self.env.action_space.shape).item()
            self.log_alpha = torch.zeros(1, requires_grad=True, device=self.device)
            self.alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=policy_lr)
            self.alpha = torch.exp(self.log_alpha)

        self.max_train_step = max_train_step
        self.eval_freq = eval_freq
        self.train_step = 0

        self.resume = resume  # whether load checkpoint start train from last time

        # BEAR
        self.lmbda = lmbda
        self.mmd_sigma = mmd_sigma
        self.kernel_type = kernel_type
        self.lagrange_thresh = lagrange_thresh
        self.n_action_samples = n_action_samples
        self.n_target_samples = n_target_samples
        self.n_mmd_action_samples = n_mmd_action_samples
        self.warmup_step = warmup_step

        # self.log_alpha_prime = torch.tensor(1.0, requires_grad=True, device=self.device)
        self.log_alpha_prime = torch.tensor(0.0, requires_grad=True, device=self.device)
        self.alpha_prime_optimizer = torch.optim.Adam([self.log_alpha_prime], lr=1e-3)

        # log dir and interval
        self.log_interval = log_interval
        self.result_dir = os.path.join(log_tools.ROOT_DIR, "run/results", train_id)
        log_tools.make_dir(self.result_dir)
        self.checkpoint_path = os.path.join(self.result_dir, "checkpoint.pth")
        self.tensorboard_writer = log_tools.TensorboardLogger(self.result_dir)

    def choose_action(self, obs, eval=False):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).reshape(1, -1).repeat(self.n_action_samples, 1).to(self.device)
            action, log_prob, mu_action = self.policy_net(obs)
            q1 = self.q_net1(obs, action)
            ind = q1.argmax(dim=0)
        return action[ind].cpu().numpy().flatten()

    def mmd_loss_laplacian(self, samples1, samples2, sigma=0.2):
        """MMD constraint with Laplacian kernel for support matching"""
        # sigma is set to 10.0 for hopper, cheetah and 20 for walker/ant
        diff_x_x = samples1.unsqueeze(2) - samples1.unsqueeze(1)  # B x N x N x d
        diff_x_x = torch.mean((-(diff_x_x.abs()).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        diff_x_y = samples1.unsqueeze(2) - samples2.unsqueeze(1)
        diff_x_y = torch.mean((-(diff_x_y.abs()).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        diff_y_y = samples2.unsqueeze(2) - samples2.unsqueeze(1)  # B x N x N x d
        diff_y_y = torch.mean((-(diff_y_y.abs()).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        overall_loss = (diff_x_x + diff_y_y - 2.0 * diff_x_y + 1e-6).sqrt()
        return overall_loss

    def mmd_loss_gaussian(self, samples1, samples2, sigma=0.2):
        """MMD constraint with Gaussian Kernel support matching"""
        # sigma is set to 10.0 for hopper, cheetah and 20 for walker/ant
        diff_x_x = samples1.unsqueeze(2) - samples1.unsqueeze(1)  # B x N x N x d
        diff_x_x = torch.mean((-(diff_x_x.pow(2)).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        diff_x_y = samples1.unsqueeze(2) - samples2.unsqueeze(1)
        diff_x_y = torch.mean((-(diff_x_y.pow(2)).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        diff_y_y = samples2.unsqueeze(2) - samples2.unsqueeze(1)  # B x N x N x d
        diff_y_y = torch.mean((-(diff_y_y.pow(2)).sum(-1)/(2.0 * sigma)).exp(), dim=(1, 2))

        overall_loss = (diff_x_x + diff_y_y - 2.0 * diff_x_y + 1e-6).sqrt()
        return overall_loss

    def train(self):
        # Sample
        batch = self.data_buffer.sample()
        obs = batch["obs"].to(self.device)
        acts = batch["acts"].to(self.device)
        rews = batch["rews"].to(self.device)
        next_obs = batch["next_obs"].to(self.device)
        done = batch["done"].to(self.device)

        """
        Train the Behaviour cloning policy to be able to take more than 1 sample for MMD.
        Conditional VAE is used as Behaviour cloning policy in BEAR.
        """
        recon_action, mu, log_std = self.cvae_net(obs, acts)
        cvae_loss = self.cvae_net.loss_function(recon_action, acts, mu, log_std)

        self.cvae_optimizer.zero_grad()
        cvae_loss.backward()
        self.cvae_optimizer.step()

        """
        Critic Training
        """
        with torch.no_grad():
            # generate 10 actions for every next_obs(Same as BCQ)
            next_obs = torch.repeat_interleave(next_obs, repeats=self.n_target_samples, dim=0).to(self.device)
            # compute target Q value of generated action
            target_q1 = self.target_q_net1(next_obs, self.policy_net(next_obs)[0])
            target_q2 = self.target_q_net2(next_obs, self.policy_net(next_obs)[0])
            # soft clipped double q-learning
            target_q = self.lmbda * torch.min(target_q1, target_q2) + (1. - self.lmbda) * torch.max(target_q1, target_q2)
            # take max over each action sampled from the generation and perturbation model
            target_q = target_q.reshape(obs.shape[0], 10, 1).max(1)[0].squeeze(1)
            target_q = rews + self.gamma * (1. - done) * target_q

        # compute current Q
        current_q1 = self.q_net1(obs, acts).squeeze(1)
        current_q2 = self.q_net2(obs, acts).squeeze(1)
        # compute critic loss
        critic_loss1 = F.mse_loss(current_q1, target_q)
        critic_loss2 = F.mse_loss(current_q2, target_q)

        self.q_optimizer1.zero_grad()
        critic_loss1.backward()
        self.q_optimizer1.step()

        self.q_optimizer2.zero_grad()
        critic_loss2.backward()
        self.q_optimizer2.step()

        # MMD Loss
        # sample actions from dataset and current policy(B x N x D)
        raw_sampled_actions = self.cvae_net.decode_multiple_without_squash(obs, decode_num=self.n_mmd_action_samples,
                                                                           z_device=self.device)
        raw_actor_actions = self.policy_net.sample_multiple_without_squash(obs, sample_num=self.n_mmd_action_samples)
        if self.kernel_type == 'gaussian':
            mmd_loss = self.mmd_loss_gaussian(raw_sampled_actions, raw_actor_actions, sigma=self.mmd_sigma)
        else:
            mmd_loss = self.mmd_loss_laplacian(raw_sampled_actions, raw_actor_actions, sigma=self.mmd_sigma)

        """
        Alpha prime training(lagrangian parameter update for MMD loss weight)
        """
        alpha_prime_loss = -(self.log_alpha_prime.exp() * (mmd_loss - self.lagrange_thresh)).mean()
        self.alpha_prime_optimizer.zero_grad()
        alpha_prime_loss.backward(retain_graph=True)
        self.alpha_prime_optimizer.step()

        self.log_alpha_prime.data.clamp_(min=-5.0, max=10.0)  # clip for stability

        """
        Actor Training
        Actor Loss = alpha_prime * (MMD Loss - threshold) + SAC Actor Loss
        """
        # SAC Actor Loss
        a, log_prob, _ = self.policy_net(obs)
        min_q = torch.min(self.q_net1(obs, a), self.q_net2(obs, a)).squeeze(1)
        policy_loss = (self.alpha * log_prob - min_q).mean()

        # BEAR Actor Loss
        actor_loss = (self.log_alpha_prime.exp() * mmd_loss).mean()
        if self.train_step > self.warmup_step:
            actor_loss = policy_loss + actor_loss
        self.policy_optimizer.zero_grad()
        actor_loss.backward()  # the mmd_loss will backward again in alpha_prime_loss.
        self.policy_optimizer.step()

        """
        Alpha training(lagrangian parameter update for SAC temperature)
        """
        if self.auto_alpha_tuning:
            alpha_loss = -(self.log_alpha * (log_prob + self.target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

            self.alpha = self.log_alpha.exp()
        else:
            alpha_loss = torch.tensor(0)

        soft_target_update(self.q_net1, self.target_q_net1, tau=self.tau)
        soft_target_update(self.q_net2, self.target_q_net2, tau=self.tau)

        self.train_step += 1

        return critic_loss1.cpu().item(), critic_loss2.cpu().item(), policy_loss.cpu().item(), alpha_loss.cpu().item(), alpha_prime_loss.cpu().item()

    def learn(self):
        if self.resume:
            self.load_agent_checkpoint()
        else:
            # delete tensorboard log file
            log_tools.del_all_files_in_dir(self.result_dir)

        while self.train_step < (int(self.max_train_step)):
            # train
            q_loss1, q_loss2, policy_loss, alpha_loss, alpha_prime_loss = self.train()

            if self.train_step % self.eval_freq == 0:
                avg_reward, avg_length = evaluate(agent=self, episode_num=5)
                self.tensorboard_writer.log_eval_data({"eval_episode_length": avg_length,
                                                       "eval_episode_reward": avg_reward}, self.train_step)

            if self.train_step % self.log_interval == 0:
                self.store_agent_checkpoint()
                self.tensorboard_writer.log_train_data({"q_loss_1": q_loss1,
                                                        "q_loss_2": q_loss2,
                                                        "policy_loss": policy_loss,
                                                        "alpha_loss": alpha_loss,
                                                        "alpha_prime_loss": alpha_prime_loss}, self.train_step)

    def store_agent_checkpoint(self):
        checkpoint = {
            "q_net1": self.q_net1.state_dict(),
            "q_net2": self.q_net2.state_dict(),
            "policy_net": self.policy_net.state_dict(),
            "q_optimizer1": self.q_optimizer1.state_dict(),
            "q_optimizer2": self.q_optimizer2.state_dict(),
            "policy_optimizer": self.policy_optimizer.state_dict(),
            "train_step": self.train_step,
        }
        if self.auto_alpha_tuning:
            checkpoint["log_alpha"] = self.log_alpha
            checkpoint["alpha_optimizer"] = self.alpha_optimizer.state_dict()

        checkpoint["log_alpha_prime"] = self.log_alpha_prime
        checkpoint["alpha_prime_optimizer"] = self.alpha_prime_optimizer.state_dict()

        torch.save(checkpoint, self.checkpoint_path)

    def load_agent_checkpoint(self):
        checkpoint = torch.load(self.checkpoint_path, map_location=self.device)  # can load gpu's data on cpu machine
        self.q_net1.load_state_dict(checkpoint["q_net1"])
        self.q_net2.load_state_dict(checkpoint["q_net2"])
        self.policy_net.load_state_dict(checkpoint["policy_net"])
        self.q_optimizer1.load_state_dict(checkpoint["q_optimizer1"])
        self.q_optimizer2.load_state_dict(checkpoint["q_optimizer2"])
        self.policy_optimizer.load_state_dict(checkpoint["policy_optimizer"])
        self.train_step = checkpoint["train_step"]

        if self.auto_alpha_tuning:
            self.log_alpha = checkpoint["log_alpha"]
            self.alpha_optimizer.load_state_dict(checkpoint["alpha_optimizer"])

        self.log_alpha_prime = checkpoint["log_alpha_prime"]
        self.alpha_prime_optimizer.load_state_dict(checkpoint["alpha_prime_optimizer"])

        print("load checkpoint from \"" + self.checkpoint_path +
              "\" at " + str(self.train_step) + " time step")