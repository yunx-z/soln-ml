import time
import numpy as np
import random as rd
from math import log, ceil
from solnml.components.utils.mfse_utils.config_space_utils import convert_configurations_to_array, \
    sample_configurations
from solnml.components.utils.mfse_utils.bohb_config_gen import BOHB
from solnml.components.utils.mfse_utils.acquisition import EI
from solnml.components.utils.mfse_utils.acq_optimizer import RandomSampling
from solnml.components.utils.mfse_utils.funcs import get_types, std_normalization
from solnml.components.utils.mfse_utils.config_space_utils import convert_configurations_to_array
from solnml.components.computation.parallel_process import ParallelProcessEvaluator
from solnml.utils.logging_utils import get_logger
from .prob_rf import RandomForestWithInstances


class BohbBase(object):
    def __init__(self, eval_func, config_space, mode='smac',
                 seed=1, R=81, eta=3, n_jobs=1):
        self.eval_func = eval_func
        self.config_space = config_space
        self.mode = mode
        self.n_workers = n_jobs

        self.trial_cnt = 0
        self.configs = list()
        self.perfs = list()
        self.incumbent_perf = float("-INF")
        self.incumbent_config = self.config_space.get_default_configuration()
        self.incumbent_configs = list()
        self.incumbent_perfs = list()
        self.global_start_time = time.time()
        self.time_ticks = list()
        self.logger = get_logger(self.__module__ + "." + self.__class__.__name__)

        # Parameters in Hyperband framework.
        self.restart_needed = True
        self.R = R
        self.eta = eta
        self.seed = seed
        self.logeta = lambda x: log(x) / log(self.eta)
        self.s_max = int(self.logeta(self.R))
        self.B = (self.s_max + 1) * self.R
        self.s_values = list(reversed(range(self.s_max + 1)))
        self.inner_iter_id = 0

        # Parameters in BOHB.
        self.iterate_r = list()
        self.target_x = dict()
        self.target_y = dict()
        self.exp_output = dict()
        for index, item in enumerate(np.logspace(0, self.s_max, self.s_max + 1, base=self.eta)):
            r = int(item)
            self.iterate_r.append(r)
            self.target_x[r] = list()
            self.target_y[r] = list()

        types, bounds = get_types(self.config_space)
        self.num_config = len(bounds)
        self.surrogate = RandomForestWithInstances(types, bounds)

        # self.executor = ParallelEvaluator(self.eval_func, n_worker=n_jobs)
        self.executor = ParallelProcessEvaluator(self.eval_func, n_worker=n_jobs)
        self.acquisition_func = EI(model=self.surrogate)
        self.acq_optimizer = RandomSampling(self.acquisition_func,
                                            self.config_space,
                                            n_samples=2000,
                                            rng=np.random.RandomState(seed))

        self.config_gen = BOHB(config_space)

        self.eval_dict = dict()

    def _iterate(self, s, skip_last=0):
        # Set initial number of configurations
        n = int(ceil(self.B / self.R / (s + 1) * self.eta ** s))
        # initial number of iterations per config
        r = int(self.R * self.eta ** (-s))

        # Choose a batch of configurations in different mechanisms.
        start_time = time.time()
        T = self.get_candidate_configurations(n)
        time_elapsed = time.time() - start_time
        self.logger.info("Choosing next batch of configurations took %.2f sec." % time_elapsed)

        for i in range((s + 1) - int(skip_last)):  # changed from s + 1

            # Run each of the n configs for <iterations>
            # and keep best (n_configs / eta) configurations

            n_configs = n * self.eta ** (-i)
            n_resource = r * self.eta ** i

            self.logger.info("BOHB: %d configurations x size %d / %d each" %
                             (int(n_configs), n_resource, self.R))

            val_losses = self.executor.parallel_execute(T, resource_ratio=float(n_resource / self.R))
            for _id, _val_loss in enumerate(val_losses):
                if np.isfinite(_val_loss):
                    self.target_x[int(n_resource)].append(T[_id])
                    self.target_y[int(n_resource)].append(_val_loss)

            self.exp_output[time.time()] = (int(n_resource), T, val_losses)

            if int(n_resource) == self.R:
                self.incumbent_configs.extend(T)
                self.incumbent_perfs.extend(val_losses)
                self.time_ticks.extend([time.time() - self.global_start_time] * len(T))

                # Only update results using maximal resources
                if self.mode != 'smac':
                    for _id, _val_loss in enumerate(val_losses):
                        if np.isfinite(_val_loss):
                            self.config_gen.new_result(T[_id], _val_loss, int(n_resource))


            # Select a number of best configurations for the next loop.
            # Filter out early stops, if any.
            indices = np.argsort(val_losses)
            if len(T) >= self.eta:
                T = [T[i] for i in indices]
                reduced_num = int(n_configs / self.eta)
                T = T[0:reduced_num]
            else:
                T = [T[indices[0]]]

        # Refit the surrogate model.
        resource_val = self.iterate_r[-1]
        if len(self.target_y[resource_val]) > 1:
            if self.mode == 'smac':
                normalized_y = std_normalization(self.target_y[resource_val])
                self.surrogate.train(convert_configurations_to_array(self.target_x[resource_val]),
                                     np.array(normalized_y, dtype=np.float64))

    def smac_get_candidate_configurations(self, num_config):
        if len(self.target_y[self.iterate_r[-1]]) <= 3:
            return sample_configurations(self.config_space, num_config)

        incumbent = dict()
        max_r = self.iterate_r[-1]
        best_index = np.argmin(self.target_y[max_r])
        incumbent['config'] = self.target_x[max_r][best_index]
        incumbent['obj'] = self.target_y[max_r][best_index]
        self.acquisition_func.update(model=self.surrogate, eta=incumbent)

        config_candidates = self.acq_optimizer.maximize(batch_size=num_config)
        p_threshold = 0.3
        candidates = list()
        idx_acq = 0
        for _id in range(num_config):
            if rd.random() < p_threshold or _id >= len(config_candidates):
                config = sample_configurations(self.config_space, 1)[0]
            else:
                config = config_candidates[idx_acq]
                idx_acq += 1
            candidates.append(config)
        return candidates

    def baseline_get_candidate_configurations(self, num_config):
        config_list = list()
        while num_config:
            config = self.config_gen.get_config(None)[0]
            if config in config_list:
                continue
            config_list.append(config)
            num_config -= 1
        return config_list

    def get_candidate_configurations(self, num_config):
        if self.mode == 'smac':
            return self.smac_get_candidate_configurations(num_config)
        else:
            return self.baseline_get_candidate_configurations(num_config)