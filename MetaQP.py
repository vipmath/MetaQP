from models import QP
from tqdm import tqdm
import matplotlib.pyplot as plt
import torch
from torch import optim
from torch.autograd import Variable
import torch.nn.functional as F
from copy import copy, deepcopy
from random import shuffle, sample
import numpy as np
from IPython.core.debugger import set_trace
from cyclic_lr import CyclicLR

import config
import utils
import model_utils
from copy import deepcopy

import os

np.seterr(all="raise")


class MetaQP:
    def __init__(self,
                 actions,
                 get_legal_actions,
                 transition_and_evaluate,
                 cuda=torch.cuda.is_available(),
                 best=False):
        utils.create_folders()

        self.cuda = cuda
        self.Q = model_utils.load_Q()
        self.P = model_utils.load_P()

        if self.cuda:
            self.Q = self.Q.cuda()
            self.P = self.Q.cuda()            

        self.actions = actions
        self.get_legal_actions = get_legal_actions
        self.transition_and_evaluate = transition_and_evaluate

        if not best:
            self.q_clr = CyclicLR(step=4*config.TRAINING_BATCH_SIZE)
            self.p_clr = CyclicLR(step=4*config.TRAINING_BATCH_SIZE)

            self.q_optim = model_utils.setup_P_optim(self.Q)
            self.p_optim = model_utils.setup_Q_optim(self.P)

            self.best_Q = model_utils.load_Q()
            self.best_P = model_utils.load_P()

            if self.cuda:
                self.best_Q = self.best_Q.cuda()
                self.best_P = self.best_P.cuda()

            self.history = utils.load_history()
            self.memories = utils.load_memories()

    def correct_policy(self, policy, state, mask=True):
        if mask:
            legal_actions = self.get_legal_actions(state[:2])

            mask = np.zeros((len(self.actions),))
            mask[legal_actions] = 1

            policy = policy * mask

        pol_sum = (np.sum(policy * 1.0))

        if pol_sum == 0:
            pass
        else:
            policy = policy / pol_sum

        return policy

    def correct_policies(self, policies, states):
        for i, (policy, state) in enumerate(zip(policies, states)):
            policies[i] = self.correct_policy(policy, state)
        return policies

    def wrap_to_variable(self, numpy_array, volatile=False):
        var = Variable(torch.from_numpy(
            numpy_array.astype("float32")), volatile=volatile)
        if self.cuda:
            var = var.cuda()
        return var

    def transition_and_evaluate_minibatch(self, minibatch, policies, tasks, num_done, is_done,
                                          bests_turn, best_starts, results):
        task_idx = 0
        n_way_idx = 0
        #map non_done minibatch indices to a smaller tensor
        non_done_view = []
        for i, (state, policy) in enumerate(zip(minibatch, policies)):
            if i % config.N_WAY == 0 and i != 0:
                task_idx += 1
            if i != 0:
                n_way_idx += 1
                n_way_idx = n_way_idx % config.N_WAY

            # this was causing this error
            # the flipping of is done is f'ing something up
            if not is_done[i]:  # and tasks[task_idx] is not None:
                action = np.random.choice(self.actions, p=policy)

                state, reward, game_over = self.transition_and_evaluate(
                    state, action)

                bests_turn = (bests_turn+1) % 2

                if game_over:
                    is_done[i] = True
                    num_done += 1
                    if results is not None:
                        for k in range(config.N_WAY-n_way_idx):
                            if not is_done[i+k] and k != 0:
                                is_done[i+k] = True
                                is_done[i] = False
                                minibatch[i] = minibatch[i+k]
                                break
                        if bests_turn == best_starts:
                            results["best"] += 1
                        else:
                            results["new"] += 1
                    else:
                        starting_player = tasks[task_idx]["starting_player"]
                        curr_player = int(state[2][0][0])
                        if starting_player != curr_player:
                            reward *= -1
                        tasks[task_idx]["memories"][n_way_idx]["result"] = reward
                else:
                    non_done_view.extend([i])

        return minibatch, tasks, num_done, is_done, results, bests_turn, non_done_view

    def get_states_from_next_minibatch(self, next_minibatch):
        states = []
        for i, state in enumerate(next_minibatch):
            if i % config.N_WAY == 0:
                states.extend([state])

        return states

    def setup_tasks(self, states, starting_player_list, episode_is_done):
        tasks = []
        minibatch = np.zeros((config.EPISODE_BATCH_SIZE,
                              config.CH, config.R, config.C))
        idx = 0
        for task_idx in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            if not episode_is_done[idx]:
                task = {
                    "state": states[task_idx],
                    "starting_player": starting_player_list[task_idx],
                    "memories": []
                }
                tasks.extend([task])
            else:
                tasks.extend([None])

            for _ in range(config.N_WAY):
                minibatch[idx] = np.array(states[task_idx])
                idx += 1

        return minibatch, tasks

    def run_episode(self, orig_states):
        np.set_printoptions(precision=3)
        results = {
            "new": 0, "best": 0, "draw": 0
        }
        states = np.array(orig_states)
        episode_is_done = []
        for _ in range(config.EPISODE_BATCH_SIZE):
            episode_is_done.extend([False])

        episode_num_done = 0

        best_starts = np.random.choice(2)

        starting_player_list = [np.random.choice(2) for _ in range(
            config.EPISODE_BATCH_SIZE//config.N_WAY)]

        if len(states) != config.CH:
            for i, state in enumerate(states):
                states[i] = np.array(state)
                states[i][2] = starting_player_list[i]
        else:
            new_states = []
            for starting_player in starting_player_list:
                new_state = np.array(states)
                new_state[2] = starting_player
                new_states.extend([new_state])
            states = new_states

        bests_turn = best_starts
        while episode_num_done < config.EPISODE_BATCH_SIZE:
            print("Num done {}".format(episode_num_done))
            states, episode_is_done, episode_num_done, results = self.meta_self_play(states=states,
                                                                                     episode_is_done=episode_is_done,
                                                                                     episode_num_done=episode_num_done,
                                                                                     results=results,
                                                                                     bests_turn=bests_turn,
                                                                                     best_starts=best_starts,
                                                                                     starting_player_list=starting_player_list)
            bests_turn = (bests_turn+1) % 2

        if len(self.memories) > config.MAX_TASK_MEMORIES:
            set_trace()
            self.memories[-config.MAX_TASK_MEMORIES:]
        utils.save_memories(self.memories)
        print("Results: ", results)
        if results["new"] > results["best"] * config.SCORING_THRESHOLD:
            model_utils.save_Q(self.Q)
            model_utils.save_P(self.P)
            print("Loading new best model")
            self.best_Q = model_utils.load_Q()
            self.best_P = model_utils.load_P()
            if self.cuda:
                self.best_Q = self.best_Q.cuda()
                self.best_P = self.best_P.cuda()
        elif results["best"] > results["new"] * config.SCORING_THRESHOLD:
            print("Reverting to previous best")
            self.Q = model_utils.load_Q()
            self.P = model_utils.load_P()
            if self.cuda:
                self.Q = self.Q.cuda()
                self.P = self.P.cuda()
            self.q_optim = model_utils.setup_Q_optim(self.Q)
            self.p_optim = model_utils.setup_P_optim(self.P)

    def meta_self_play(self, states, episode_is_done, episode_num_done, bests_turn,
                       results, best_starts, starting_player_list):
        self.Q.eval()
        self.P.eval()
        self.best_Q.eval()
        self.best_P.eval()
        
        minibatch, tasks = self.setup_tasks(
            states=states,
            starting_player_list=starting_player_list,
            episode_is_done=episode_is_done)

        minibatch_variable = self.wrap_to_variable(minibatch)

        if bests_turn == 1:
            Q = self.best_Q
            P = self.best_P
            P_optim = model_utils.setup_P_optim(P)
        else:
            Q = self.Q
            P = self.P

        #So let me think, I need
        #So let me think what I want
        #I want the optimal policy under the Q
        #i.e. I want to do training with the policy until it is basically optimal, and I dont care if it overfits
        #We can have a separate training for the policy, i.e. we can regress it towards the found optimal policies, or we can do a single update or something
        #to avoid underfitting. so basically, make a copy of the policy net, train it for lets say 10 iterations
        #use that new policy as the "improved" policy. save it for later to match the policy net towards that training
        #mix in one policy to be dirichlet noise so that we have some randomness and in theory can see all states and policies

        torch.save(P.state_dict(), "temp")

        optimal_value_tensor = np.ones(
            (config.EPISODE_BATCH_SIZE//config.N_WAY, 1))

        optimal_value_target = self.wrap_to_variable(optimal_value_tensor)  

        #What do I need for this
        #I need a variety of policies
        #for example P when Q = 1, P when Q = -1, and a linspace inbetween
        #and probably one dirichlet noise policy mixed in
        #so lets say we have those 6 policies, when N_WAY=5
        #We want to get an estimate of how good those policies are
        #so basically we want to do one playout of those policies under the current "optimal" policy
        #In order to do the linspace idea we would need to have the policy not just learn the optimal policy
        #but rather learn the policy associated with a certain value
        #so it learns how to reproduce the various policies.
        #one issue with it is that until we get a good policy function -1 and 1 might be very similar
        #and we won't get a good variety
        #perhap we can just keep it simple and do one random policy and one "optimal" policy
        #so we will in thoery be able to see everything
        #and then maybe the N_WAY is how good the estimate would be, i.e. how results we would get
        #so basically the current system, but we add one dirichlet noise policy
        #and 

        #so yeah basically have N_WAY be the number of estimates we get the value of the state
        #the alternative is we can set N_WAY=1, and add a random policy estimate
        #because it is a bit redundant to get multiple samples, maybe not idk.

        #what is the alternative with training to convergence for the current Q and state
        #it in theory will produce a strong state which we can regress the policy net towards
        #or we can even put the training for the policy net in here, although it is not recommended

        #How would that work
        #basically we would get a set of strong policies for each of the states
        #Under this we could use N_WAY to get multiple estimates of the value for each policy/state
        #or we can do N_WAY=1 and treat each idx in the batch as it's own thing

        #we need to mix in at least one dirichlet random policy,
        #so for example maybe the strongest play results 

        #What do we need. We need to create a strong Q function which gets a fairly accurate Q policy function
        #Doing a 5_way policy evaluation will be more accurate, but it may overfit and be redundant
        
        #So basically we overfit all of the current states and get the strongest current policy, then we evaluate half those and half random dirichlet policies
        #Only the current strongest policy will count towards the final

        #So of course the issue with this is that at the end of the day we need to have the policy functin which will do the intermediate action choices to be good so we arent getting
        #a super noisy estimate. To do that we can either save these "optimal" P's and use them, or we could train the P for more training loops

        #Just having the policy learn separately is basically what I'm doing now so it doesn't make a ton of sense.
        for _ in range(config.NUM_SELF_PLAY_UPDATES):
            P.train()
            P.zero_grad()

            policies = P(minibatch_variable)
            qs = Q(minibatch_variable, policies)

            policy_loss = F.mse_loss(qs, optimal_value_target)

            policy_loss.backward()

            P_optim.step()

        P.load_state_dict(torch.load("temp"))
        P.eval()  

        policies = policies.detach().data.numpy()

        corrected_policies = self.correct_policies(policies, minibatch)

        idx = 0
        for task_idx in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            for _ in range(config.N_WAY):
                if not episode_is_done[idx]:
                    tasks[task_idx]["memories"].extend(
                        [{"policy": corrected_policies[idx]}])
                elif tasks[task_idx] is not None:
                    tasks[task_idx]["memories"].extend([None])
                idx += 1

        is_done = deepcopy(episode_is_done)
        num_done = episode_num_done

        next_minibatch, tasks, \
            episode_num_done, episode_is_done, \
            results, bests_turn, non_done_view = self.transition_and_evaluate_minibatch(minibatch=np.array(minibatch),
                                                                         policies=corrected_policies,
                                                                         tasks=tasks,
                                                                         num_done=episode_num_done,
                                                                         is_done=episode_is_done,
                                                                         bests_turn=bests_turn,
                                                                         best_starts=best_starts,
                                                                         results=results)

        next_states = self.get_states_from_next_minibatch(next_minibatch)
        # revert back to orig turn now that we are done
        bests_turn = (bests_turn+1) % 2

        policies = corrected_policies

        while True:
            minibatch, tasks, \
                num_done, is_done, \
                _, bests_turn, non_done_view = self.transition_and_evaluate_minibatch(minibatch=minibatch,
                                                                    policies=policies,
                                                                    tasks=tasks,
                                                                    num_done=num_done,
                                                                    is_done=is_done,
                                                                    bests_turn=bests_turn,
                                                                    best_starts=best_starts,
                                                                    results=None)

            if num_done == config.EPISODE_BATCH_SIZE:
                break
            
            minibatch_view = minibatch[non_done_view]

            minibatch_view_variable = self.wrap_to_variable(minibatch_view)

            # when you fixed this use is_done to make a view of the minibatch_variable which will reduce the batch size going into
            # pytorch when you have some that are done, i.e. removing redundancy. perhaps put it in transition and evaluate with an option

            if bests_turn == 1:
                Q = self.best_Q
                P = self.best_P
            else:
                Q = self.Q
                P = self.P

            # Idea: since I am going through a trajectory of states, I could probably
            # also learn a value function and have the Q value for the original policy
            # be a combination of the V and the reward. so basically we could use the V
            # function in a couple different ways. for the main moves we could use it
            # to scale the policies according to the V values from the transitioned states,
            # i.e. for each of the transitioned states from the improved policies, we
            # look at the V values from those, and scale the action probas according to those
            # so basically we could rescale it to 0-1 and then multiply it with the policies
            # and it should increase the probabilities for estimatedly good actions and
            # decrease for bad ones

            # for the inner loop Q estimation trajectories we could average together the V
            # values for each of the states, i.e. we could have an additional target
            # for the Q network, which is the averaged together V values from the trajectory
            # that should provide a fairly good estimate of the Q value, and won't be
            # as noisy as the result

            # another possible improvement is making the policy noise learnable, i.e.
            # the scale of the noise, and how much weight it has relative to the generated policy
            policies_view = self.P(minibatch_view_variable)

            policies_view = policies_view.detach().data.numpy()

            policies_view = self.correct_policies(policies_view, minibatch_view)

            policies[non_done_view] = policies_view
        fixed_tasks = []
        for _, task in enumerate(tasks):
            if task is not None:
                new_memories = []
                for i, memory in enumerate(task["memories"]):
                    if memory is not None:
                        new_memories.extend([memory])

                task["memories"] = new_memories
                fixed_tasks.extend([task])

        self.memories.extend(fixed_tasks)

        return next_states, episode_is_done, episode_num_done, results

    def train_Q(self, minibatch):
        self.Q.train()
        self.P.train()
        self.q_optim.zero_grad()   
        self.p_optim.zero_grad()        

        lr = self.q_clr.get_rate()
        model_utils.adjust_learning_rate(self.q_optim, lr)
        # print("Q lr: {}".format(lr))     

        results_tensor = np.zeros((config.TRAINING_BATCH_SIZE, 1))

        policies_tensor = np.zeros((
            config.TRAINING_BATCH_SIZE, config.R * config.C))

        batch_task_tensor = np.zeros((config.TRAINING_BATCH_SIZE,
                                      config.CH, config.R, config.C))

        idx = 0
        for _, task in enumerate(minibatch):
            for memory in task["memories"]:
                results_tensor[idx] = memory["result"]
                policies_tensor[idx] = memory["policy"]
                batch_task_tensor[idx] = task["state"]
                idx += 1

        state_input = self.wrap_to_variable(batch_task_tensor)
        policies_input = self.wrap_to_variable(policies_tensor)
        results_target = self.wrap_to_variable(results_tensor)

        Qs = self.Q(state_input, policies_input)

        Q_loss = F.mse_loss(Qs, results_target)

        Q_loss.backward()

        self.q_optim.step()

        return Q_loss.data.numpy()[0]

    def train_P(self, minibatch):
        self.Q.train()
        self.P.train()
        self.p_optim.zero_grad()
        self.q_optim.zero_grad()

        lr = self.p_clr.get_rate()
        model_utils.adjust_learning_rate(self.q_optim, lr)
        # print("P lr: {}".format(lr))    

        batch_task_tensor = np.zeros((config.TRAINING_BATCH_SIZE,
                                      config.CH, config.R, config.C))

        idx = 0
        for i, task in enumerate(minibatch):
            for memory in task["memories"]:
                batch_task_tensor[idx] = task["state"]
                idx += 1

        policies_view = []
        for i in range(config.TRAINING_BATCH_SIZE):
            if i % config.N_WAY == 0:
                policies_view.extend([i])

        improved_policies_tensor = np.zeros((
            config.TRAINING_BATCH_SIZE//config.N_WAY, config.R * config.C))

        optimal_value_tensor = np.ones(
            (config.TRAINING_BATCH_SIZE//config.N_WAY, 1)) 

        state_input = self.wrap_to_variable(batch_task_tensor) 
        improved_policies_target = self.wrap_to_variable(improved_policies_tensor)   
        optimal_value_target = self.wrap_to_variable(optimal_value_tensor)    

        policies = self.P(state_input)
        Qs = self.Q(state_input, policies)

        policies_smaller = policies[policies_view]

        improved_policy_loss = 0
        for improved_policy, policy in zip(improved_policies_target, policies_smaller):
            improved_policy = improved_policy.unsqueeze(0)
            policy = policy.unsqueeze(-1)
            improved_policy_loss += -torch.mm(improved_policy,
                                            torch.log(policy))

        improved_policy_loss /= len(policies_smaller)

        Qs_smaller = Qs[policies_view]

        policy_loss = improved_policy_loss + \
            F.mse_loss(Qs_smaller, optimal_value_target)

        policy_loss.backward()

        self.p_optim.step()

        return policy_loss.data.numpy()[0]

    def train(self,
        epochs=config.EPOCHS,
        training_loops=config.TRAINING_LOOPS):
        if (len(self.memories) < config.MIN_TASK_MEMORIES):
            return
        num_batches = config.TRAINING_BATCH_SIZE//config.N_WAY

        for _ in range(training_loops):
            minibatch = sample(self.memories, min(num_batches, len(self.memories)))

            for _ in range(config.Q_EPOCHS):
                self.history["Q"].extend([self.train_Q(minibatch)])
            
            for _ in range(config.P_EPOCHS):
                self.history["P"].extend([self.train_P(minibatch)])

            print("Q_loss: {}".format(self.history["Q"][-1]), 
            "P_loss: {}".format(self.history["P"][-1][0]))

        utils.save_history(self.history)

    def find_lr(self, model, optim, name, starting_max_lr=.001, starting_min_lr=.00005):
        print("Finding {} lr".format(name))
        model_utils.save_temp(model, name)
        
        loss = 1e8
        last_loss = 1e9

        lr = starting_max_lr
        num_batches = config.TRAINING_BATCH_SIZE//config.N_WAY

        minibatches = [sample(self.memories, min(num_batches, len(self.memories))) for _ 
         in range(config.TRAINING_LOOPS)]

        step_size = config.TRAINING_LOOPS*config.EPOCHS*8 #*2

        multiplier = 2

        while loss < last_loss:
            summed_loss = 0
            last_loss = loss
            last_lr = lr
            print(name, lr)

            if name is "Q":
                self.Q = model_utils.load_temp(name)
                self.q_optim = model_utils.setup_Q_optim(self.Q)
                self.Q.train()
                self.q_clr = CyclicLR(base_lr = lr/8, max_lr=lr, step=step_size*config.Q_UPDATES_PER) 

                for i in range(config.TRAINING_LOOPS):
                    minibatch = minibatches[i]

                    for _ in range(config.EPOCHS):
                        summed_loss += self.train_Q(minibatch)
                         
            elif name is "P":
                self.P = model_utils.load_temp(name)
                self.p_optim = model_utils.setup_P_optim(self.P)
                self.P.train()
                self.p_clr = CyclicLR(base_lr = lr/8, max_lr=lr, step=step_size) 
                
                for i in range(config.TRAINING_LOOPS):
                    minibatch = minibatches[i]

                    for _ in range(config.EPOCHS):
                        summed_loss += self.train_P(minibatch)
            else:
                raise "Model name is wrong"

            lr *= multiplier
            loss = summed_loss/(config.TRAINING_LOOPS*config.EPOCHS)
        # max_lr = last_lr
        # loss = 1e8
        # last_loss = 1e9
        # lr = starting_min_lr
        # while loss < last_loss:
        #     summed_loss = 0
        #     last_loss = loss
        #     last_lr = lr
        #     print(name, lr)

        #     if name is "Q":
        #         self.Q = model_utils.load_temp(name)
        #         self.q_optim = model_utils.setup_Q_optim(self.Q)
        #         self.Q.train()
        #         self.q_clr = CyclicLR(base_lr = lr, max_lr=max_lr, step=step_size) 

        #         idx = 0
        #         for i in range(config.TRAINING_LOOPS):
        #             minibatch = minibatches[i]

        #             for _ in range(config.EPOCHS):
        #                 summed_loss += self.train_Q(minibatch, iter = idx, 
        #                     iterations=iterations)
        #                 idx += 1
                         
        #     elif name is "P":
        #         self.P = model_utils.load_temp(name)
        #         self.p_optim = model_utils.setup_P_optim(self.P)
        #         self.P.train()
        #         self.p_clr = CyclicLR(base_lr = lr, max_lr=max_lr, step=step_size) 
                
        #         idx = 0
        #         for i in range(config.TRAINING_LOOPS):
        #             minibatch = minibatches[i]

        #             for e in range(config.EPOCHS):
        #                 summed_loss += self.train_P(minibatch, iter = idx, 
        #                 iterations=iterations)
        #     else:
        #         raise "Model name is wrong"

        #     lr *= multiplier
        #     loss = summed_loss/(config.TRAINING_LOOPS*config.EPOCHS)

        # min_lr = last_lr
        max_lr = last_lr
        min_lr = last_lr/8
        if name is "Q":
            self.Q = model_utils.load_temp(name)
            self.q_optim = model_utils.setup_Q_optim(self.Q)
            self.q_clr = CyclicLR(base_lr = min_lr, max_lr=max_lr, 
                step=step_size*config.Q_UPDATES_PER)
        else:
            self.P = model_utils.load_temp(name)
            self.p_optim = model_utils.setup_P_optim(self.P)
            self.p_clr = CyclicLR(base_lr = min_lr, max_lr=max_lr, 
                step=step_size)

    def find_lrs(self):
        if len(self.memories) < config.MIN_TASK_MEMORIES:
            return
        self.find_lr(self.Q, self.q_optim, "Q", starting_max_lr=.048)
        self.find_lr(self.P, self.p_optim, "P", starting_max_lr=.002)



    def meta_sgd(self):
        task_distribution = self.memories #p(T)
        #learning rate stored in CLR

        meta_net = MetaSGD(theta, alpha)

        for _ in range(config.TRAINING_LOOPS):
            #so let me see 
            #train(T_i) represents the training set for a task t_i
            #so in effect we are taking a task, i.e. one memory, and splitting into a
            #train and test, so basically we 

            #so basically get a batch
            #for each memory in the batch take out 20% of the examples and set them aside to be
            #a test batch.

            #we update the net with the train batch
            #and we test the net on the test batch
            #we use a learned tanh gating function from the neural net
            #which will control the direction and magnitude of the gradient update
            #one for each example in the index I would assume
            #so if we have a batch size of 100 we would learn 100 tanh's which will
            #choose how to update.... hmmm
            #Idk I need to see how they do it.


            #
            train_loss = meta_net()

        return theta, alpha