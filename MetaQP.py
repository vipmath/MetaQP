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

np.seterr(all="raise")


class MetaQP:
    def __init__(self,
                 actions,
                 calculate_reward,
                 get_legal_actions,
                 transition,
                 cuda=torch.cuda.is_available(),
                 best=False):
        utils.create_folders()

        self.cuda = cuda
        self.qp = model_utils.load_model()
        if self.cuda:
            self.qp = self.qp.cuda()

        self.actions = actions
        self.get_legal_actions = get_legal_actions
        self.calculate_reward = calculate_reward
        self.transition = transition

        if not best:
            self.q_optim, self.p_optim = model_utils.setup_optims(self.qp)
            self.best_qp = model_utils.load_model()

            if self.cuda:
                self.best_qp = self.best_qp.cuda()

            self.history = utils.load_history()
            self.memories = utils.load_memories()

    def correct_policy(self, policy, state):
        legal_actions = self.get_legal_actions(state[:2])

        mask = np.zeros(((len(self.actions)), len(self.actions)))
        mask[legal_actions] = 1

        policy = policy * mask

        pol_sum = (np.sum(policy * 1.0))

        if pol_sum == 0:
            pass
        else:
            policy = policy / pol_sum

        return policy

    def correct_policies(self, policies, state):
        for policy in policies:
            policy = self.correct_policy(policy, state)
        return policies

    def get_improved_task_policies_list(self, policies):
        improved_policies = []
        for i in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            improved_policy = policies[i:i + config.N_WAY].sum()
            improved_policies.extend([improved_policy])

        return improved_policies

    def wrap_to_variable(self, tensor, volatile=False):
        var = Variable(torch.from_numpy(tensor), volatile=volatile)
        if self.cuda:
            var = var.cuda()
        return var

    def create_task_tensor(self, state):
        # a task is going to be from the perspective of a certain player
        # so we want to

        # np.array to make a fast copy
        state = np.array(np.expand_dims(state, 0))
        n_way_state = np.repeat(state, config.N_WAY, axis=0)
        n_way_state_tensor = torch.from_numpy(n_way_state)

        return n_way_state_tensor

    def update_task_memories(self, tasks, corrected_final_policies, improved_task_policies):
        for i, task in enumerate(tasks):
            task["improved_policy"] = improved_task_policies[i]
            for policy in corrected_final_policies[i:i + config.N_WAY]:
                task["memories"].extend({"policy": policy})

        return task

    # so what are the targets going to be
    # the Q will get a set of states and policies (maybe a mix of the initial policy, and
    # the corrected_policy). It's goal to generalize Q values for that state

    # the policy net will get one example will get a small aux loss driving it to
    # the corrected policy, and maybe we will have a meta policy prediction later
    # if we do the first idea

    # so we can have it where the training net always goes first, then the best net
    # always goes second. we randomly choose the starting player for the input states,
    # and the new and best nets should get an even number of games for player 1 / 2

    # so let me think about this some more, all I really need are states, some slightly
    # different policies, and the results.

    def mix_task_policies(self, improved_task_policies, policies, perc_improved=0):
        for i, improved_policy in enumerate(improved_task_policies):
            for policy in policies[i:i + config.N_WAY]:
                policy = policy(1 - perc_improved) + improved_policy * perc_improved

        return policies

    def transition_batch_task_tensor(self, batch_task_tensor,
                                     corrected_final_policies, is_done):
        for i, (state, policy) in enumerate(zip(batch_task_tensor,
                                                corrected_final_policies)):
            if not is_done[i]:
                action = np.random.choice(self.actions, p=policy)
                state = self.transition(state[:2], action)

        return batch_task_tensor

    def check_finished_games(self, batch_task_tensor, is_done, tasks, num_done):
        idx = 0
        for j in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            for i, state in enumerate(batch_task_tensor[j:j + config.N_WAY]):
                if not is_done[idx]:
                    legal_actions = self.get_legal_actions(state[:2])
                    if len(legal_actions) == 0:
                        is_done[idx] = True
                        num_done += 1
                        tasks[j][idx]["result"] = 0
                    else:
                        reward, game_over = self.calculate_reward(state[:2])

                        if game_over:
                            is_done[idx] = True
                            curr_player = state[2][0]
                            if tasks[j]["starting_player"] != curr_player:
                                reward *= -1
                            tasks[j]["memories"][idx]["result"] = reward
                            num_done += 1

                idx += 1

        return is_done, tasks, num_done

    def meta_self_play(self, state):
        # fast copy it
        state = np.array(state)
        self.qp.eval()
        self.best_qp.eval()
        tasks = []
        batch_task_tensor = torch.zeros(config.EPISODE_BATCH_SIZE,
                                        config.CH, config.R, config.C)

        for i in range(config.EPISODE_BATCH_SIZE // config.N_WAY):
            # starting player chosen randomly
            starting_player = np.random.choice(1)
            state[2] = starting_player
            task_tensor = self.create_task_tensor(state)
            batch_task_tensor[i * config.N_WAY] = task_tensor

            task = {
                "state": task_tensor, "starting_player": starting_player, "memories": []
            }

        batch_task_variable = self.wrap_to_variable(batch_task_tensor)

        best_start = np.random.choice(1)

        if best_start == 1:
            qp = self.best_qp
        else:
            qp = self.qp

        qs, policies = qp(batch_task_variable, percent_random=.2)

        # scales from -1 to 1 to 0 to 1
        scaled_qs = (qs + 1) / 2

        weighted_policies = policies * scaled_qs

        improved_task_policies = self.get_improved_task_policies_list(
            weighted_policies)

        #***Idea***
        #since the initial policy will be for the first state, we could average 
        #the whole batch and argmax to pick the next initial state,
        #effect following a very strong trajectory, and maybe biasing play
        #towards better results?
        #Although we are naturally seeing early states a lot more since those are the seeds
        #for trajectories. So, the policy should be especially good for those

        # well in theory since the orig policies are partially random and
        # the final policy is random, using only the improved policy might be fine
        # will set it like that for now. it will lead to a bit less
        # diversity in the policies that the Q sees, which is kind of bad.
        # but then again, we will get more of a true Q value for that policy.
        # we can try it out for now. ill set to .8 so some difference happens
        final_policies = self.mix_task_policies(improved_task_policies,
                                                policies, perc_improved=1)

        corrected_final_policies = self.correct_policies(final_policies, state)

        tasks = self.update_task_memories(
            tasks, corrected_final_policies, improved_task_policies)

        is_done = []
        for i in range(config.EPISODE_BATCH_SIZE):
            is_done.extend([False])

        # sooo let me think. the new_net and best_net will continually trade off batch
        # evaluations. basically the new_net chooses some initial_moves, and
        # then it alternates until all the games are done. #this will bias that the new
        # net always makes the first move, which can be significant
        # so now it's random start. so the opposing moves for each turn will be chosen
        # by the opposite net
        num_done = 0
        if best_start == 1:
            best_turn = 0
        else:
            best_turn = 1

        results = {
            "new": 0, "best": 0, "draw": 0
        }

        while num_done < config.EPISODE_BATCH_SIZE:
            batch_task_tensor = self.transition_batch_task_tensor(batch_task_tensor,
                                                                  corrected_final_policies, is_done)

            is_done, tasks, results, num_done = self.check_finished_games(batch_task_tensor, is_done,
                                                                          tasks, num_done)

            batch_task_variable = self.wrap_to_variable(batch_task_tensor)

            if best_turn == 1:
                qp = self.best_qp
            else:
                qp = self.qp

            _, policies = self.qp(batch_task_variable)

            policies = self.correct_policies(policies)

        self.memories.extend(tasks)
        if len(self.memories) > config.MAX_TASK_MEMORIES:
            self.memories[-config.MAX_TASK_MEMORIES:]
        utils.save_memories()

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

    def train_memories(self):
        self.qp.train()

        # so memories are a list of lists containing memories
        if len(self.memories) < config.MIN_TASK_MEMORIES:
            print("Need {} tasks, have {}".format(
                config.MIN_TASK_MEMORIES, len(self.memories)))
            return

        for _ in range(config.TRAINING_LOOPS):
            tasks = sample(self.memories, config.SAMPLE_SIZE)

            BATCH_SIZE = config.TRAINING_BATCH_SIZE // config.N_WAY
            extra = config.SAMPLE_SIZE - config.SAMPLE_SIZE % BATCH_SIZE
            minibatches = [
                tasks[x:x + BATCH_SIZE]
                for x in range(0, len(tasks) - extra, BATCH_SIZE)
            ]
            self.train_tasks(minibatches)

        # self.train_minibatches(minibatches)

    def train_tasks(self, minibatches_of_tasks):
        batch_task_tensor = torch.zeros(config.TRAINING_BATCH_SIZE,
                                        config.CH, config.R, config.C)

        result_tensor = torch.zeros(config.TRAINING_BATCH_SIZE, 1)

        policies_tensor = torch.zeros(
            config.TRAINING_BATCH_SIZE, config.R * config.C)

        improved_policies_variable = self.wrap_to_variable(
            torch.zeros(config.N_WAY, config.R * config.C))

        optimal_value_tensor = torch.ones(config.TRAINING_BATCH_SIZE, 1)

        for mb in minibatches_of_tasks:
            self.q_optim.zero_grad()
            self.p_optim.zero_grad()

            Q_loss = 0
            policy_loss = 0

            policy_view = []

            idx = 0
            for i, task in enumerate(mb):
                state = task["state"]
                improved_policies_variable[i] = task["improved_policy"]
                policy_view.extend([i])

                for memory in task["memory"]:
                    result = memory["result"]

                    policies_tensor[idx] = memory["policy"]
                    batch_task_tensor[idx] = state
                    idx += 1

            state_input = self.wrap_to_variable(batch_task_tensor)
            policies_input = self.wrap_to_variable(policies_tensor)
            Qs, _ = self.qp(state_input, policies_input)

            Q_loss += F.mse_loss(Qs, result)

            self.q_optim.step()

            Qs, policies = self.qp(state_input)

            optimal_value_var = self.wrap_to_variable(optimal_value_tensor)
            policy_loss += F.mse_loss(Qs, optimal_value_var)
            policy_loss += torch.mm(improved_policies_variable.t(),
                                    torch.log(policies[policy_view]))

            self.p_optim.step()

            total_loss = Q_loss + policy_loss
            total_loss.backward()
