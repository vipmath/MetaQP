from models import QP
from tqdm import tqdm
import matplotlib.pyplot as plt
import torch
from torch import optim
from torch.autograd import Variable
import torch.nn.functional as F
from copy import copy
from random import shuffle, sample
import numpy as np
from IPython.core.debugger import set_trace

import config
import utils
import model_utils
from copy import deepcopy

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
        self.qp = model_utils.load_model()
        if self.cuda:
            self.qp = self.qp.cuda()

        self.actions = actions
        self.get_legal_actions = get_legal_actions
        self.transition_and_evaluate = transition_and_evaluate

        if not best:
            self.q_optim, self.p_optim = model_utils.setup_optims(self.qp)
            self.best_qp = model_utils.load_model()

            if self.cuda:
                self.best_qp = self.best_qp.cuda()

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
            self.memories[-config.MAX_TASK_MEMORIES:]
        utils.save_memories(self.memories)
        print("Results: ", results)
        if results["new"] > results["best"] * config.SCORING_THRESHOLD:
            model_utils.save_model(self.qp)
            print("Loading new best model")
            self.best_qp = model_utils.load_model()
            if self.cuda:
                self.best_qp = self.best_qp.cuda()
        elif results["best"] > results["new"] * config.SCORING_THRESHOLD:
            print("Reverting to previous best")
            self.qp = model_utils.load_model()
            if self.cuda:
                self.qp = self.qp.cuda()
            self.q_optim, self.p_optim = model_utils.setup_optims(self.qp)

    def meta_self_play(self, states, episode_is_done, episode_num_done, bests_turn,
                       results, best_starts, starting_player_list):
        self.qp.eval()
        self.best_qp.eval()
        minibatch, tasks = self.setup_tasks(
            states=states,
            starting_player_list=starting_player_list,
            episode_is_done=episode_is_done)

        minibatch_variable = self.wrap_to_variable(minibatch)

        if bests_turn == 1:
            qp = self.best_qp
        else:
            qp = self.qp

        _, policies = qp(minibatch_variable, percent_random=.2)

        policies = policies.detach().data.numpy()

        corrected_policies = self.correct_policies(policies, minibatch)

        # corrected_policies_copy = np.array(corrected_policies)

        policies_input = self.wrap_to_variable(corrected_policies)

        qs, _ = qp(minibatch_variable, policies_input)

        qs = qs.detach().data.numpy()

        idx = 0
        for task_idx in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            for _ in range(config.N_WAY):
                #if tasks[task_idx] is not None:
                if not episode_is_done[idx]:
                    tasks[task_idx]["memories"].extend(
                        [{"policy": corrected_policies[idx]}])
                elif tasks[task_idx] is not None:
                    tasks[task_idx]["memories"].extend([None])
                idx += 1

        scaled_qs = (qs + 1) / 2
        weighted_policies = corrected_policies * scaled_qs

        idx = 0
        for task_idx in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            summed_policy = 0
            for _ in range(config.N_WAY):
                summed_policy += weighted_policies[idx]
                idx += 1
            idx -= config.N_WAY

            improved_policy = self.correct_policy(
                summed_policy, minibatch[idx], mask=True)

            if tasks[task_idx] is not None:
                tasks[task_idx]["improved_policy"] = improved_policy
            for _ in range(config.N_WAY):
                weighted_policies[idx] = improved_policy
                idx += 1

        is_done = deepcopy(episode_is_done)
        num_done = episode_num_done

        improved_policies = weighted_policies

        next_minibatch, tasks, \
            episode_num_done, episode_is_done, \
            results, bests_turn, non_done_view = self.transition_and_evaluate_minibatch(minibatch=np.array(minibatch),
                                                                         policies=improved_policies,
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
                qp = self.best_qp
            else:
                qp = self.qp

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
            _, policies_view = self.qp(minibatch_view_variable)

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

    def train_memories(self):
        self.qp.train()
        self.qp.Q.train()
        self.qp.P.train()
        self.qp.StateModule.train()

        # so memories are a list of lists containing memories
        if len(self.memories) < config.MIN_TASK_MEMORIES:
            print("Need {} tasks, have {}".format(
                config.MIN_TASK_MEMORIES, len(self.memories)))
            return

        for _ in tqdm(range(config.TRAINING_LOOPS)):
            # tasks = sample(self.memories, config.SAMPLE_SIZE)
            minibatch = sample(self.memories, 
                min(config.TRAINING_BATCH_SIZE//config.N_WAY, len(self.memories)))

            # BATCH_SIZE = config.TRAINING_BATCH_SIZE // config.N_WAY
            # extra = config.SAMPLE_SIZE % BATCH_SIZE
            # minibatches = [
            #     tasks[x:x + BATCH_SIZE]
            #     for x in range(0, len(tasks) - extra, BATCH_SIZE)
            # ]
            self.train_tasks(minibatch)

        utils.save_history(self.history)

        # self.train_minibatches(minibatches)

    def train_tasks(self, minibatch):
        batch_task_tensor = np.zeros((config.TRAINING_BATCH_SIZE,
                                      config.CH, config.R, config.C))

        policies_view = []
        for i in range(config.TRAINING_BATCH_SIZE):
            if i % config.N_WAY == 0:
                policies_view.extend([i])

        result_tensor = np.zeros((config.TRAINING_BATCH_SIZE, 1))

        policies_tensor = np.zeros((
            config.TRAINING_BATCH_SIZE, config.R * config.C))

        improved_policies_tensor = np.zeros((
            config.TRAINING_BATCH_SIZE//config.N_WAY, config.R * config.C))

        optimal_value_tensor = np.ones(
            (config.TRAINING_BATCH_SIZE//config.N_WAY, 1))

        idx = 0
        for i, task in enumerate(minibatch):
            state = task["state"]
            improved_policies_tensor[i] = task["improved_policy"]

            for memory in task["memories"]:
                #note: as of right now the memories could be less that N_WAY
                #so we are using partially zero tensors.
                #this could be a major issue for thing like MSE error
                result_tensor[idx] = memory["result"]

                policies_tensor[idx] = memory["policy"]
                batch_task_tensor[idx] = state
                idx += 1

        result_tensor = result_tensor[:idx]
        policies_tensor = policies_tensor[:idx]
        batch_task_tensor = batch_task_tensor[:idx]
        improved_policies_tensor = improved_policies_tensor[:idx//config.N_WAY]
        optimal_value_tensor = optimal_value_tensor[:idx//config.N_WAY]

        policies_view = policies_view[:idx//config.N_WAY]        
        #so lets say we have 20 tasks
        #and we only have 80 memories
        #we want the 80 to get the same transform
        #so 80//config.N_WAY = 16
        state_input = self.wrap_to_variable(batch_task_tensor)
        policies_input = self.wrap_to_variable(policies_tensor)
        improved_policies_target = self.wrap_to_variable(
            improved_policies_tensor)
        result_target = self.wrap_to_variable(result_tensor)

        optimal_value_var = self.wrap_to_variable(optimal_value_tensor)

        for e in range(config.EPOCHS):
            self.q_optim.zero_grad()
            self.p_optim.zero_grad()

            for _ in range(config.Q_UPDATES_PER):
                Q_loss = 0

                Qs, _ = self.qp(state_input, policies_input)

                Q_loss += F.mse_loss(Qs, result_target)*10

                Q_loss.backward()

                self.q_optim.step()

                self.q_optim.zero_grad()
            # self.p_optim.zero_grad() #should be redundant
            policy_loss = 0

            Qs, policies = self.qp(state_input)

            # corrected_policy_loss = 0
            # for corrected_policy, policy in zip(policies_input, policies):
            #     corrected_policy = corrected_policy.unsqueeze(0)
            #     policy = policy.unsqueeze(-1)
            #     corrected_policy_loss += -torch.mm(corrected_policy,
            #                                         torch.log(policy))
            # corrected_policy_loss /= 3*len(policies_input)

            policies_smaller = policies[policies_view]

            improved_policy_loss = 0
            for improved_policy, policy in zip(improved_policies_target, policies_smaller):
                improved_policy = improved_policy.unsqueeze(0)
                policy = policy.unsqueeze(-1)
                improved_policy_loss += -torch.mm(improved_policy,
                                                torch.log(policy))

            improved_policy_loss /= len(policies_smaller)

            Qs_smaller = Qs[policies_view]

            # policy_loss = corrected_policy_loss +
            policy_loss = improved_policy_loss*5 #+ \
                #F.mse_loss(Qs_smaller, optimal_value_var)*2

            #/ and * 2 to balance improved policies matching and regression

            # for _ in range(config.TRAINING_BATCH_SIZE):
            # Qs, policies = self.qp(state_input)
            # policy_loss += F.mse_loss(Qs, optimal_value_var)

            policy_loss.backward()
            # policies.grad
            # set_trace()

            self.p_optim.step()
            p_loss = policy_loss.data.numpy()[0]
            q_loss = Q_loss.data.numpy()[0]
            self.history["q_loss"].extend([q_loss])
            self.history["p_loss"].extend([p_loss])

            if e == (config.EPOCHS-1):
                print("Policy loss {}".format(policy_loss.data.numpy()[0]))
                print("Q loss: {}".format(Q_loss.data.numpy()[0]))
