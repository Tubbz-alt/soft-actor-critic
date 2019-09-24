import sys, os
import numpy as np
import time
import gym

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
import tensorflow as tf
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

from core import *
from spinup.utils.logx import EpochLogger


class ReplayBuffer:
    """
    A simple FIFO experience replay buffer for SAC agents.
    """

    def __init__(self, obs_dim, act_dim, size):
        self.obs1_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.obs2_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.acts_buf = np.zeros([size, act_dim], dtype=np.float32)
        self.rews_buf = np.zeros(size, dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size

    def store(self, obs, act, rew, next_obs, done):
        self.obs1_buf[self.ptr] = obs
        self.obs2_buf[self.ptr] = next_obs
        self.acts_buf[self.ptr] = act
        self.rews_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr+1) % self.max_size
        self.size = min(self.size+1, self.max_size)

    def sample_batch(self, batch_size=32):
        idxs = np.random.randint(0, self.size, size=batch_size)
        return dict(obs1=self.obs1_buf[idxs],
                    obs2=self.obs2_buf[idxs],
                    acts=self.acts_buf[idxs],
                    rews=self.rews_buf[idxs],
                    done=self.done_buf[idxs])


def process_observation(o, obs_dim, observation_type):
    if observation_type == 'Discrete':
        o = np.eye(obs_dim)[o]
    return o

def process_action(a, act_dim):
    one_hot_a = np.eye(act_dim)[a]
    return one_hot_a

def process_reward(reward):
    # apply clipping here if needed
    return reward

"""

Soft Actor-Critic

(With slight variations that bring it closer to TD3)

"""

def sac(env_fn, actor_critic=a_out_mlp_actor_critic,
                logger_kwargs=dict(),
                network_params=dict(),
                rl_params=dict()):

    logger = EpochLogger(**logger_kwargs)
    logger.save_config(locals())

    seed            = rl_params['seed']
    epochs          = rl_params['epochs']
    steps_per_epoch = rl_params['steps_per_epoch']
    replay_size     = rl_params['replay_size']
    alpha           = rl_params['alpha']
    target_entropy  = rl_params['target_entropy']
    gamma           = rl_params['gamma']
    polyak          = rl_params['polyak']
    lr              = rl_params['lr']
    batch_size      = rl_params['batch_size']
    start_steps     = rl_params['start_steps']
    max_ep_len      = rl_params['max_ep_len']
    save_freq       = rl_params['save_freq']
    render          = rl_params['render']

    tf.set_random_seed(seed)
    np.random.seed(seed)

    env, test_env = env_fn(), env_fn()
    obs_space = env.observation_space
    act_space = env.action_space

    try:
        obs_dim = obs_space.n
        observation_type = 'Discrete'
    except AttributeError as e:
        obs_dim = obs_space.shape[0]
        observation_type = 'Box'

    act_dim = act_space.n

    # Inputs to computation graph
    x_ph, a_ph, x2_ph, r_ph, d_ph = placeholders(obs_dim, act_dim, obs_dim, None, None)

    # alpha Params
    if target_entropy == 'auto': # discrete case should be target_entropy < ln(act_dim)
        target_entropy = tf.log(tf.cast(act_dim, tf.float32)) * 0.5
    else:
        target_entropy = tf.cast(target_entropy, tf.float32)

    log_alpha = tf.get_variable('log_alpha', dtype=tf.float32, initializer=target_entropy)

    if alpha == 'auto': # auto tune alpha
        alpha = tf.exp(log_alpha)
    else: # fixed alpha
        alpha = tf.get_variable('alpha', dtype=tf.float32, initializer=alpha)


    # Main outputs from computation graph
    with tf.variable_scope('main'):
        mu, pi, pi_entropy, pi_logits, q1_logits, q2_logits, q1_a, q2_a, q1_pi, q2_pi = actor_critic(x_ph, a_ph, alpha, act_space, **network_params)

    # Target value network
    with tf.variable_scope('target'):
        _, _, pi_entropy_targ, _, _, _,  _, _, q1_pi_targ, q2_pi_targ = actor_critic(x2_ph, a_ph, alpha, act_space, **network_params)

    # Experience buffer
    replay_buffer = ReplayBuffer(obs_dim=obs_dim, act_dim=act_dim, size=replay_size)

    # Count variables
    var_counts = tuple(count_vars(scope) for scope in
                       ['log_alpha', 'main/pi', 'main/q1', 'main/q2', 'main'])
    print(('\nNumber of parameters: \t alpha: %d, \t pi: %d, \t' + \
           'q1: %d, \t q2: %d, \t total: %d\n')%var_counts)

    # Min Double-Q:
    min_q_logits  = tf.minimum(q1_logits, q2_logits)
    min_q_pi_targ = tf.minimum(q1_pi_targ, q2_pi_targ)

    # Targets for Q regression
    q_backup = r_ph + gamma*(1-d_ph)*tf.stop_gradient(min_q_pi_targ + alpha * pi_entropy_targ)

    # critic losses
    q1_loss = 0.5 * tf.reduce_mean((q_backup - q1_a)**2)
    q2_loss = 0.5 * tf.reduce_mean((q_backup - q2_a)**2)
    value_loss = q1_loss + q2_loss

    # cross entropy method (D_KL = H(P,Q) - H(P))
    pi_action_probs    = tf.nn.softmax(pi_logits, axis=-1)
    q_log_action_probs = tf.nn.log_softmax(min_q_logits, axis=-1)
    pi_q_cross_entropy = -tf.reduce_sum(pi_action_probs * q_log_action_probs, axis=-1)
    pi_loss = pi_q_cross_entropy - alpha*pi_entropy

    # alpha loss for temperature parameter
    alpha_backup = tf.stop_gradient(target_entropy - pi_entropy)
    alpha_loss   = -tf.reduce_mean(log_alpha * alpha_backup)

    # Policy train op
    # (has to be separate from value train op, because q1_pi appears in pi_loss)
    pi_optimizer = tf.train.AdamOptimizer(learning_rate=lr, epsilon=1e-08)
    train_pi_op = pi_optimizer.minimize(pi_loss, var_list=get_vars('main/pi'))

    # Value train op
    # (control dep of train_pi_op because sess.run otherwise evaluates in nondeterministic order)
    value_optimizer = tf.train.AdamOptimizer(learning_rate=lr, epsilon=1e-08)
    with tf.control_dependencies([train_pi_op]):
        train_value_op = value_optimizer.minimize(value_loss, var_list=get_vars('main/q'))

    alpha_optimizer = tf.train.AdamOptimizer(learning_rate=lr, epsilon=1e-08)
    with tf.control_dependencies([train_value_op]):
        train_alpha_op = alpha_optimizer.minimize(alpha_loss, var_list=get_vars('log_alpha'))

    # Polyak averaging for target variables
    # (control flow because sess.run otherwise evaluates in nondeterministic order)
    with tf.control_dependencies([train_value_op]):
        target_update = tf.group([tf.assign(v_targ, polyak*v_targ + (1-polyak)*v_main)
                                  for v_main, v_targ in zip(get_vars('main'), get_vars('target'))])

    # All ops to call during one training step
    step_ops = [pi_loss, q1_loss, q2_loss, q1_a, q2_a,
                pi_entropy, target_entropy,
                alpha_loss, alpha,
                train_pi_op, train_value_op, train_alpha_op, target_update]

    # Initializing targets to match main variables
    target_init = tf.group([tf.assign(v_targ, v_main)
                              for v_main, v_targ in zip(get_vars('main'), get_vars('target'))])

    sess = tf.Session()
    sess.run(tf.global_variables_initializer())
    sess.run(target_init)

    # Setup model saving
    logger.setup_tf_saver(sess, inputs={'x': x_ph, 'a': a_ph},
                                outputs={'pi': pi, 'q1': q1_a, 'q2': q2_a})

    def get_action(o, deterministic=False):
        act_op = mu if deterministic else pi
        return sess.run(act_op, feed_dict={x_ph: [o]})[0]

    def test_agent(n=10, render=True):
        global sess, mu, pi, q1_a, q2_a, q1_pi, q2_pi
        for j in range(n):
            o, r, d, ep_ret, ep_len = test_env.reset(), 0, False, 0, 0
            o = process_observation(o, obs_dim, observation_type)

            if render: test_env.render()

            while not(d or (ep_len == max_ep_len)):
                # Take deterministic actions at test time
                o, r, d, _ = test_env.step(get_action(o, True))
                o = process_observation(o, obs_dim, observation_type)
                ep_ret += r
                ep_len += 1

                if render: test_env.render()

            if render: test_env.close()
            logger.store(TestEpRet=ep_ret, TestEpLen=ep_len)

    start_time = time.time()
    o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0
    o = process_observation(o, obs_dim, observation_type)
    total_steps = steps_per_epoch * epochs

    # Main loop: collect experience in env and update/log each epoch
    for t in range(total_steps):
        """
        Until start_steps have elapsed, randomly sample actions
        from a uniform distribution for better exploration. Afterwards,
        use the learned policy.
        """
        if t > start_steps:
            a = get_action(o)
        else:
            a = env.action_space.sample()

        # Step the env
        o2, r, d, _ = env.step(a)
        o2 = process_observation(o2, obs_dim, observation_type)
        a = process_action(a, act_dim)
        ep_ret += r
        ep_len += 1

        # Ignore the "done" signal if it comes from hitting the time
        # horizon (that is, when it's an artificial terminal signal
        # that isn't based on the agent's state)
        d = False if ep_len==max_ep_len else d

        # Store experience to replay buffer
        replay_buffer.store(o, a, r, o2, d)

        # Super critical, easy to overlook step: make sure to update
        # most recent observation!
        o = o2

        if d or (ep_len == max_ep_len):
            """
            Perform all SAC updates at the end of the trajectory.
            This is a slight difference from the SAC specified in the
            original paper.
            """
            for j in range(ep_len):
                batch = replay_buffer.sample_batch(batch_size)
                feed_dict = {x_ph:  batch['obs1'],
                             x2_ph: batch['obs2'],
                             a_ph:  batch['acts'],
                             r_ph:  batch['rews'],
                             d_ph:  batch['done'],
                            }
                            
                outs = sess.run(step_ops, feed_dict)
                logger.store(LossPi=outs[0],
                             LossQ1=outs[1],    LossQ2=outs[2],
                             Q1Vals=outs[3],    Q2Vals=outs[4],
                             PiEntropy=outs[5], TargEntropy=outs[6],
                             LossAlpha=outs[7], Alpha=outs[8])

            logger.store(EpRet=ep_ret, EpLen=ep_len)
            o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0
            o = process_observation(o, obs_dim, observation_type)


        # End of epoch wrap-up
        if t > 0 and t % steps_per_epoch == 0:
            epoch = t // steps_per_epoch

            # Save model
            if (epoch % save_freq == 0) or (epoch == epochs-1):
                logger.save_state({'env': env}, None)

            # Test the performance of the deterministic version of the agent.
            test_agent(n=10,render=render)

            # Log info about epoch
            logger.log_tabular('Epoch', epoch)
            logger.log_tabular('EpRet', with_min_and_max=True)
            logger.log_tabular('TestEpRet', with_min_and_max=True)
            logger.log_tabular('EpLen', average_only=True)
            logger.log_tabular('TestEpLen', average_only=True)
            logger.log_tabular('TotalEnvInteracts', t)
            logger.log_tabular('Q1Vals', with_min_and_max=True)
            logger.log_tabular('Q2Vals', with_min_and_max=True)
            logger.log_tabular('PiEntropy', average_only=True)
            logger.log_tabular('TargEntropy', average_only=True)
            logger.log_tabular('Alpha', average_only=True)
            logger.log_tabular('LossPi', average_only=True)
            logger.log_tabular('LossQ1', average_only=True)
            logger.log_tabular('LossQ2', average_only=True)
            logger.log_tabular('LossAlpha', average_only=True)
            logger.log_tabular('Time', time.time()-start_time)
            logger.dump_tabular()


if __name__ == '__main__':

    from spinup.utils.run_utils import setup_logger_kwargs

    network_params = {
        'hidden_sizes':[64, 64],
        'activation':'relu',
        'policy':kl_policy
    }

    rl_params = {
        'env_name':'FrozenLake-v0',
        # 'env_name':'CartPole-v1',
        # 'env_name':'Taxi-v2',
        # 'env_name':'MountainCar-v0',
        # 'env_name':'Acrobot-v1',
        # 'env_name':'LunarLander-v2',
        'seed': int(123),
        'epochs': int(300),
        'steps_per_epoch': 2000,
        'replay_size': 100000,
        'alpha': 'auto',
        'target_entropy':'auto', # fixed or auto define with act_dim
        'gamma': 0.95,
        'polyak': 0.995,
        'lr': 1e-3,
        'batch_size': 128,
        'start_steps': 4000,
        'max_ep_len': 500,
        'save_freq': 5,
        'render': False
    }

    saved_model_dir = '../saved_models'
    logger_kwargs = setup_logger_kwargs(exp_name='discrete_sac_kl_' + rl_params['env_name'], seed=rl_params['seed'], data_dir=saved_model_dir, datestamp=False)
    env = gym.make(rl_params['env_name'])

    sac(lambda:env, actor_critic=a_out_mlp_actor_critic,
                    logger_kwargs=logger_kwargs,
                    network_params=network_params,
                    rl_params=rl_params)
