import copy
import os
import pickle
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from functools import partial
from unittest.mock import patch

import gym
import numpy as np
import pytest
import ray
from ray import tune
from ray.air import CheckpointConfig, ScalingConfig
from ray.air._internal.remote_storage import _ensure_directory
from ray.rllib import _register_all
from ray.tune import (
    register_env,
    register_trainable,
    run,
    run_experiments,
    Trainable,
    TuneError,
    Stopper,
)
from ray.tune.callback import Callback
from ray.tune.experiment import Experiment
from ray.tune.trainable import wrap_function
from ray.tune.logger import Logger, LegacyLoggerCallback
from ray.tune.experiment.trial import _noop_logger_creator
from ray.tune.result import (
    TIMESTEPS_TOTAL,
    DONE,
    HOSTNAME,
    NODE_IP,
    PID,
    EPISODES_TOTAL,
    TRAINING_ITERATION,
    TIMESTEPS_THIS_ITER,
    TIME_THIS_ITER_S,
    TIME_TOTAL_S,
    TRIAL_ID,
    EXPERIMENT_TAG,
)
from ray.tune.schedulers import (
    TrialScheduler,
    FIFOScheduler,
    AsyncHyperBandScheduler,
)
from ray.tune.schedulers.pb2 import PB2
from ray.tune.stopper import (
    MaximumIterationStopper,
    TrialPlateauStopper,
    ExperimentPlateauStopper,
)
from ray.tune.search import BasicVariantGenerator, grid_search, ConcurrencyLimiter
from ray.tune.search._mock import _MockSuggestionAlgorithm
from ray.tune.search.ax import AxSearch
from ray.tune.search.hyperopt import HyperOptSearch
from ray.tune.syncer import Syncer
from ray.tune.experiment import Trial
from ray.tune.execution.trial_runner import TrialRunner
from ray.tune.utils import flatten_dict
from ray.tune.execution.placement_groups import PlacementGroupFactory


class TrainableFunctionApiTest(unittest.TestCase):
    def setUp(self):
        ray.init(num_cpus=4, num_gpus=0, object_store_memory=150 * 1024 * 1024)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        ray.shutdown()
        _register_all()  # re-register the evicted objects
        shutil.rmtree(self.tmpdir)

    def checkAndReturnConsistentLogs(self, results, sleep_per_iter=None):
        """Checks logging is the same between APIs.

        Ignore "DONE" for logging but checks that the
        scheduler is notified properly with the last result.
        """
        class_results = copy.deepcopy(results)
        function_results = copy.deepcopy(results)

        class_output = []
        function_output = []
        scheduler_notif = []

        class MockScheduler(FIFOScheduler):
            def on_trial_complete(self, runner, trial, result):
                scheduler_notif.append(result)

        class ClassAPILogger(Logger):
            def on_result(self, result):
                class_output.append(result)

        class FunctionAPILogger(Logger):
            def on_result(self, result):
                function_output.append(result)

        class _WrappedTrainable(Trainable):
            def setup(self, config):
                del config
                self._result_iter = copy.deepcopy(class_results)

            def step(self):
                if sleep_per_iter:
                    time.sleep(sleep_per_iter)
                res = self._result_iter.pop(0)  # This should not fail
                if not self._result_iter:  # Mark "Done" for last result
                    res[DONE] = True
                return res

        def _function_trainable(config, reporter):
            for result in function_results:
                if sleep_per_iter:
                    time.sleep(sleep_per_iter)
                reporter(**result)

        class_trainable_name = "class_trainable"
        register_trainable(class_trainable_name, _WrappedTrainable)

        [trial1] = run(
            _function_trainable,
            callbacks=[LegacyLoggerCallback([FunctionAPILogger])],
            raise_on_failed_trial=False,
            scheduler=MockScheduler(),
        ).trials

        [trial2] = run(
            class_trainable_name,
            callbacks=[LegacyLoggerCallback([ClassAPILogger])],
            raise_on_failed_trial=False,
            scheduler=MockScheduler(),
        ).trials

        trials = [trial1, trial2]

        # Ignore these fields
        NO_COMPARE_FIELDS = {
            HOSTNAME,
            NODE_IP,
            TRIAL_ID,
            EXPERIMENT_TAG,
            PID,
            TIME_THIS_ITER_S,
            TIME_TOTAL_S,
            DONE,  # This is ignored because FunctionAPI has different handling
            "timestamp",
            "time_since_restore",
            "experiment_id",
            "date",
        }

        self.assertEqual(len(class_output), len(results))
        self.assertEqual(len(function_output), len(results))

        def as_comparable_result(result):
            return {k: v for k, v in result.items() if k not in NO_COMPARE_FIELDS}

        function_comparable = [
            as_comparable_result(result) for result in function_output
        ]
        class_comparable = [as_comparable_result(result) for result in class_output]

        self.assertEqual(function_comparable, class_comparable)

        self.assertEqual(sum(t.get(DONE) for t in scheduler_notif), 2)
        self.assertEqual(
            as_comparable_result(scheduler_notif[0]),
            as_comparable_result(scheduler_notif[1]),
        )

        # Make sure the last result is the same.
        self.assertEqual(
            as_comparable_result(trials[0].last_result),
            as_comparable_result(trials[1].last_result),
        )

        return function_output, trials

    def testRegisterEnv(self):
        register_env("foo", lambda: None)
        self.assertRaises(TypeError, lambda: register_env("foo", 2))

    def testRegisterEnvOverwrite(self):
        def train(config, reporter):
            reporter(timesteps_total=100, done=True)

        def train2(config, reporter):
            reporter(timesteps_total=200, done=True)

        register_trainable("f1", train)
        register_trainable("f1", train2)
        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                }
            }
        )
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TIMESTEPS_TOTAL], 200)

    def testRegisterTrainable(self):
        def train(config, reporter):
            pass

        class A:
            pass

        class B(Trainable):
            pass

        register_trainable("foo", train)
        Experiment("test", train)
        register_trainable("foo", B)
        Experiment("test", B)
        self.assertRaises(TypeError, lambda: register_trainable("foo", B()))
        self.assertRaises(TuneError, lambda: Experiment("foo", B()))
        self.assertRaises(TypeError, lambda: register_trainable("foo", A))
        self.assertRaises(TypeError, lambda: Experiment("foo", A))

    def testRegisterTrainableThrice(self):
        def train(config, reporter):
            pass

        register_trainable("foo", train)
        register_trainable("foo", train)
        register_trainable("foo", train)

    def testTrainableCallable(self):
        def dummy_fn(config, reporter, steps):
            reporter(timesteps_total=steps, done=True)

        from functools import partial

        steps = 500
        register_trainable("test", partial(dummy_fn, steps=steps))
        [trial] = run_experiments(
            {
                "foo": {
                    "run": "test",
                }
            }
        )
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TIMESTEPS_TOTAL], steps)
        [trial] = tune.run(partial(dummy_fn, steps=steps)).trials
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TIMESTEPS_TOTAL], steps)

    def testBuiltInTrainableResources(self):
        class B(Trainable):
            @classmethod
            def default_resource_request(cls, config):
                return PlacementGroupFactory(
                    [{"CPU": config["cpu"], "GPU": config["gpu"]}]
                )

            def step(self):
                return {"timesteps_this_iter": 1, "done": True}

        register_trainable("B", B)

        def f(cpus, gpus):
            return run_experiments(
                {
                    "foo": {
                        "run": "B",
                        "config": {
                            "cpu": cpus,
                            "gpu": gpus,
                        },
                    }
                },
            )[0]

        # TODO(xwjiang): https://github.com/ray-project/ray/issues/19959
        # self.assertEqual(f(0, 0).status, Trial.TERMINATED)

        # TODO(xwjiang): Make FailureInjectorCallback a test util.
        class FailureInjectorCallback(Callback):
            """Adds random failure injection to the TrialExecutor."""

            def __init__(self, steps=4):
                self._step = 0
                self.steps = steps

            def on_step_begin(self, iteration, trials, **info):
                self._step += 1
                if self._step >= self.steps:
                    raise RuntimeError

        def g(cpus, gpus):
            return run_experiments(
                {
                    "foo": {
                        "run": "B",
                        "config": {
                            "cpu": cpus,
                            "gpu": gpus,
                        },
                    }
                },
                callbacks=[FailureInjectorCallback()],
            )[0]

        # Too large resource requests are infeasible
        # TODO(xwjiang): Throw TuneError after https://github.com/ray-project/ray/issues/19985.  # noqa
        os.environ["TUNE_WARN_INSUFFICENT_RESOURCE_THRESHOLD_S"] = "0"

        with self.assertRaises(RuntimeError), patch.object(
            ray.tune.execution.ray_trial_executor.logger, "warning"
        ) as warn_mock:
            self.assertRaises(TuneError, lambda: g(100, 100))
            assert warn_mock.assert_called_once()

        with self.assertRaises(RuntimeError), patch.object(
            ray.tune.execution.ray_trial_executor.logger, "warning"
        ) as warn_mock:
            self.assertRaises(TuneError, lambda: g(0, 100))
            assert warn_mock.assert_called_once()

        with self.assertRaises(RuntimeError), patch.object(
            ray.tune.execution.ray_trial_executor.logger, "warning"
        ) as warn_mock:
            self.assertRaises(TuneError, lambda: g(100, 0))
            assert warn_mock.assert_called_once()

    def testRewriteEnv(self):
        def train(config, reporter):
            reporter(timesteps_total=1)

        register_trainable("f1", train)

        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                    "env": "CartPole-v0",
                }
            }
        )
        self.assertEqual(trial.config["env"], "CartPole-v0")

    def testConfigPurity(self):
        def train(config, reporter):
            assert config == {"a": "b"}, config
            reporter(timesteps_total=1)

        register_trainable("f1", train)
        run_experiments(
            {
                "foo": {
                    "run": "f1",
                    "config": {"a": "b"},
                }
            }
        )

    def testLogdir(self):
        def train(config, reporter):
            assert (
                os.path.join(ray._private.utils.get_user_temp_dir(), "logdir", "foo")
                in os.getcwd()
            ), os.getcwd()
            reporter(timesteps_total=1)

        register_trainable("f1", train)
        run_experiments(
            {
                "foo": {
                    "run": "f1",
                    "local_dir": os.path.join(
                        ray._private.utils.get_user_temp_dir(), "logdir"
                    ),
                    "config": {"a": "b"},
                }
            }
        )

    def testLogdirStartingWithTilde(self):
        local_dir = "~/ray_results/local_dir"

        def train(config, reporter):
            cwd = os.getcwd()
            assert cwd.startswith(os.path.expanduser(local_dir)), cwd
            assert not cwd.startswith("~"), cwd
            reporter(timesteps_total=1)

        register_trainable("f1", train)
        run_experiments(
            {
                "foo": {
                    "run": "f1",
                    "local_dir": local_dir,
                    "config": {"a": "b"},
                }
            }
        )

    def testLongFilename(self):
        def train(config, reporter):
            assert (
                os.path.join(ray._private.utils.get_user_temp_dir(), "logdir", "foo")
                in os.getcwd()
            ), os.getcwd()
            reporter(timesteps_total=1)

        register_trainable("f1", train)
        run_experiments(
            {
                "foo": {
                    "run": "f1",
                    "local_dir": os.path.join(
                        ray._private.utils.get_user_temp_dir(), "logdir"
                    ),
                    "config": {
                        "a" * 50: tune.sample_from(lambda spec: 5.0 / 7),
                        "b" * 50: tune.sample_from(lambda spec: "long" * 40),
                    },
                }
            }
        )

    def testBadParams(self):
        def f():
            run_experiments({"foo": {}})

        self.assertRaises(TuneError, f)

    def testBadParams2(self):
        def f():
            run_experiments(
                {
                    "foo": {
                        "run": "asdf",
                        "bah": "this param is not allowed",
                    }
                }
            )

        self.assertRaises(TuneError, f)

    def testBadParams3(self):
        def f():
            run_experiments(
                {
                    "foo": {
                        "run": grid_search("invalid grid search"),
                    }
                }
            )

        self.assertRaises(TuneError, f)

    def testBadParams4(self):
        def f():
            run_experiments(
                {
                    "foo": {
                        "run": "asdf",
                    }
                }
            )

        self.assertRaises(TuneError, f)

    def testBadParams5(self):
        def f():
            run_experiments({"foo": {"run": "__fake", "stop": {"asdf": 1}}})

        self.assertRaises(TuneError, f)

    def testBadParams6(self):
        def f():
            run_experiments({"foo": {"run": "PPO", "resources_per_trial": {"asdf": 1}}})

        self.assertRaises(TuneError, f)

    def testBadStoppingReturn(self):
        def train(config, reporter):
            reporter()

        register_trainable("f1", train)

        def f():
            run_experiments(
                {
                    "foo": {
                        "run": "f1",
                        "stop": {"time": 10},
                    }
                }
            )

        self.assertRaises(TuneError, f)

    def testNestedStoppingReturn(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test={"test1": {"test2": i}})

        with self.assertRaises(TuneError):
            [trial] = tune.run(train, stop={"test": {"test1": {"test2": 6}}}).trials
        [trial] = tune.run(train, stop={"test/test1/test2": 6}).trials
        self.assertEqual(trial.last_result["training_iteration"], 7)

    def testStoppingFunction(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test=i)

        def stop(trial_id, result):
            return result["test"] > 6

        [trial] = tune.run(train, stop=stop).trials
        self.assertEqual(trial.last_result["training_iteration"], 8)

    def testStoppingMemberFunction(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test=i)

        class Stopclass:
            def stop(self, trial_id, result):
                return result["test"] > 6

        [trial] = tune.run(train, stop=Stopclass().stop).trials
        self.assertEqual(trial.last_result["training_iteration"], 8)

    def testStopper(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test=i)

        class CustomStopper(Stopper):
            def __init__(self):
                self._count = 0

            def __call__(self, trial_id, result):
                print("called")
                self._count += 1
                return result["test"] > 6

            def stop_all(self):
                return self._count > 5

        trials = tune.run(train, num_samples=5, stop=CustomStopper()).trials
        self.assertTrue(all(t.status == Trial.TERMINATED for t in trials))
        self.assertTrue(
            any(t.last_result.get("training_iteration") is None for t in trials)
        )

    def testEarlyStopping(self):
        def train(config, reporter):
            reporter(test=0)

        top = 3

        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", top=0)
        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", top="0")
        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", std=0)
        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", patience=-1)
        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", std="0")
        with self.assertRaises(ValueError):
            ExperimentPlateauStopper("test", mode="0")

        stopper = ExperimentPlateauStopper("test", top=top, mode="min")

        analysis = tune.run(train, num_samples=10, stop=stopper)
        self.assertTrue(all(t.status == Trial.TERMINATED for t in analysis.trials))
        self.assertTrue(len(analysis.dataframe(metric="test", mode="max")) <= top)

        patience = 5
        stopper = ExperimentPlateauStopper(
            "test", top=top, mode="min", patience=patience
        )

        analysis = tune.run(train, num_samples=20, stop=stopper)
        self.assertTrue(all(t.status == Trial.TERMINATED for t in analysis.trials))
        self.assertTrue(len(analysis.dataframe(metric="test", mode="max")) <= patience)

        stopper = ExperimentPlateauStopper("test", top=top, mode="min")

        analysis = tune.run(train, num_samples=10, stop=stopper)
        self.assertTrue(all(t.status == Trial.TERMINATED for t in analysis.trials))
        self.assertTrue(len(analysis.dataframe(metric="test", mode="max")) <= top)

    def testBadStoppingFunction(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test=i)

        class CustomStopper:
            def stop(self, result):
                return result["test"] > 6

        def stop(result):
            return result["test"] > 6

        with self.assertRaises(TuneError):
            tune.run(train, stop=CustomStopper().stop)
        with self.assertRaises(TuneError):
            tune.run(train, stop=stop)

    def testMaximumIterationStopper(self):
        def train(config):
            for i in range(10):
                tune.report(it=i)

        stopper = MaximumIterationStopper(max_iter=6)

        out = tune.run(train, stop=stopper)
        self.assertEqual(out.trials[0].last_result[TRAINING_ITERATION], 6)

    def testTrialPlateauStopper(self):
        def train(config):
            tune.report(10.0)
            tune.report(11.0)
            tune.report(12.0)
            for i in range(10):
                tune.report(20.0)

        # num_results = 4, no other constraints --> early stop after 7
        stopper = TrialPlateauStopper(metric="_metric", num_results=4)

        out = tune.run(train, stop=stopper)
        self.assertEqual(out.trials[0].last_result[TRAINING_ITERATION], 7)

        # num_results = 4, grace period 9 --> early stop after 9
        stopper = TrialPlateauStopper(metric="_metric", num_results=4, grace_period=9)

        out = tune.run(train, stop=stopper)
        self.assertEqual(out.trials[0].last_result[TRAINING_ITERATION], 9)

        # num_results = 4, min_metric = 22 --> full 13 iterations
        stopper = TrialPlateauStopper(
            metric="_metric", num_results=4, metric_threshold=22.0, mode="max"
        )

        out = tune.run(train, stop=stopper)
        self.assertEqual(out.trials[0].last_result[TRAINING_ITERATION], 13)

    def testCustomTrialDir(self):
        def train(config):
            for i in range(10):
                tune.report(test=i)

        custom_name = "TRAIL_TRIAL"

        def custom_trial_dir(trial):
            return custom_name

        trials = tune.run(
            train,
            config={"t1": tune.grid_search([1, 2, 3])},
            trial_dirname_creator=custom_trial_dir,
            local_dir=self.tmpdir,
        ).trials
        logdirs = {t.local_path for t in trials}
        assert len(logdirs) == 3
        assert all(custom_name in dirpath for dirpath in logdirs)

    def testTrialDirRegression(self):
        def train(config, reporter):
            for i in range(10):
                reporter(test=i)

        trials = tune.run(
            train, config={"t1": tune.grid_search([1, 2, 3])}, local_dir=self.tmpdir
        ).trials
        logdirs = {t.local_path for t in trials}
        for i in [1, 2, 3]:
            assert any(f"t1={i}" in dirpath for dirpath in logdirs)
        for t in trials:
            assert any(t.trainable_name in dirpath for dirpath in logdirs)

    def testEarlyReturn(self):
        def train(config, reporter):
            reporter(timesteps_total=100, done=True)
            time.sleep(99999)

        register_trainable("f1", train)
        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                }
            }
        )
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TIMESTEPS_TOTAL], 100)

    def testReporterNoUsage(self):
        def run_task(config, reporter):
            print("hello")

        experiment = Experiment(run=run_task, name="ray_crash_repro")
        [trial] = ray.tune.run(experiment).trials
        print(trial.last_result)
        self.assertEqual(trial.last_result[DONE], True)

    def testRerun(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmpdir))

        def test(config):
            tid = config["id"]
            fail = config["fail"]
            marker = os.path.join(tmpdir, f"t{tid}-{fail}.log")
            if not os.path.exists(marker) and fail:
                open(marker, "w").close()
                raise ValueError
            for i in range(10):
                time.sleep(0.1)
                tune.report(hello=123)

        config = dict(
            name="hi-2",
            config={
                "fail": tune.grid_search([True, False]),
                "id": tune.grid_search(list(range(5))),
            },
            verbose=1,
            local_dir=tmpdir,
        )
        trials = tune.run(test, raise_on_failed_trial=False, **config).trials
        self.assertEqual(Counter(t.status for t in trials)["ERROR"], 5)
        new_trials = tune.run(test, resume="ERRORED_ONLY", **config).trials
        self.assertEqual(Counter(t.status for t in new_trials)["ERROR"], 0)
        self.assertTrue(all(t.last_result.get("hello") == 123 for t in new_trials))

    # Test rerunning rllib trials with ERRORED_ONLY.
    def testRerunRlLib(self):
        class TestEnv(gym.Env):
            counter = 0

            def __init__(self, config):
                self.observation_space = gym.spaces.Discrete(1)
                self.action_space = gym.spaces.Discrete(1)
                TestEnv.counter += 1

            def reset(self):
                return 0

            def step(self, act):
                return [0, 1, True, {}]

        class FailureInjectionCallback(Callback):
            def on_step_end(self, **info):
                raise RuntimeError

        with self.assertRaises(Exception):
            tune.run(
                "PPO",
                config={
                    "env": TestEnv,
                    "framework": "torch",
                    "num_workers": 0,
                },
                name="my_experiment",
                callbacks=[FailureInjectionCallback()],
                stop={"training_iteration": 1},
            )
        trials = tune.run(
            "PPO",
            config={
                "env": TestEnv,
                "framework": "torch",
                "num_workers": 0,
            },
            name="my_experiment",
            resume="ERRORED_ONLY",
            stop={"training_iteration": 1},
        ).trials
        assert len(trials) == 1 and trials[0].status == Trial.TERMINATED

    def testTrialInfoAccess(self):
        class TestTrainable(Trainable):
            def step(self):
                result = {
                    "name": self.trial_name,
                    "trial_id": self.trial_id,
                    "trial_resources": self.trial_resources,
                }
                print(result)
                return result

        analysis = tune.run(
            TestTrainable,
            stop={TRAINING_ITERATION: 1},
            resources_per_trial=PlacementGroupFactory([{"CPU": 1}]),
        )
        trial = analysis.trials[0]
        self.assertEqual(trial.last_result.get("name"), str(trial))
        self.assertEqual(trial.last_result.get("trial_id"), trial.trial_id)
        self.assertEqual(
            trial.last_result.get("trial_resources"), trial.placement_group_factory
        )

    def testTrialInfoAccessFunction(self):
        def train(config, reporter):
            reporter(
                name=reporter.trial_name,
                trial_id=reporter.trial_id,
                trial_resources=reporter.trial_resources,
            )

        analysis = tune.run(
            train,
            stop={TRAINING_ITERATION: 1},
            resources_per_trial=PlacementGroupFactory([{"CPU": 1}]),
        )
        trial = analysis.trials[0]
        self.assertEqual(trial.last_result.get("name"), str(trial))
        self.assertEqual(trial.last_result.get("trial_id"), trial.trial_id)
        self.assertEqual(
            trial.last_result.get("trial_resources"), trial.placement_group_factory
        )

        def track_train(config):
            tune.report(
                name=tune.get_trial_name(),
                trial_id=tune.get_trial_id(),
                trial_resources=tune.get_trial_resources(),
            )

        analysis = tune.run(
            track_train,
            stop={TRAINING_ITERATION: 1},
            resources_per_trial=PlacementGroupFactory([{"CPU": 1}]),
        )
        trial = analysis.trials[0]
        self.assertEqual(trial.last_result.get("name"), str(trial))
        self.assertEqual(trial.last_result.get("trial_id"), trial.trial_id)
        self.assertEqual(
            trial.last_result.get("trial_resources"), trial.placement_group_factory
        )

    def testLotsOfStops(self):
        class TestTrainable(Trainable):
            def step(self):
                result = {"name": self.trial_name, "trial_id": self.trial_id}
                return result

            def cleanup(self):
                time.sleep(0.3)
                open(os.path.join(self.logdir, "marker"), "a").close()
                return 1

        analysis = tune.run(TestTrainable, num_samples=10, stop={TRAINING_ITERATION: 1})
        for trial in analysis.trials:
            path = os.path.join(trial.local_path, "marker")
            assert os.path.exists(path)

    def testReportTimeStep(self):
        # Test that no timestep count are logged if never the Trainable never
        # returns any.
        results1 = [dict(mean_accuracy=5, done=i == 99) for i in range(100)]
        logs1, _ = self.checkAndReturnConsistentLogs(results1)

        self.assertTrue(all(TIMESTEPS_TOTAL not in log for log in logs1))

        # Test that no timesteps_this_iter are logged if only timesteps_total
        # are returned.
        results2 = [dict(timesteps_total=5, done=i == 9) for i in range(10)]
        logs2, _ = self.checkAndReturnConsistentLogs(results2)

        # Re-run the same trials but with added delay. This is to catch some
        # inconsistent timestep counting that was present in the multi-threaded
        # FunctionTrainable. This part of the test can be removed once the
        # multi-threaded FunctionTrainable is removed from ray/tune.
        # TODO: remove once the multi-threaded function runner is gone.
        logs2, _ = self.checkAndReturnConsistentLogs(results2, 0.5)

        # check all timesteps_total report the same value
        self.assertTrue(all(log[TIMESTEPS_TOTAL] == 5 for log in logs2))
        # check that none of the logs report timesteps_this_iter
        self.assertFalse(any(hasattr(log, TIMESTEPS_THIS_ITER) for log in logs2))

        # Test that timesteps_total and episodes_total are reported when
        # timesteps_this_iter and episodes_this_iter are provided by user,
        # despite only return zeros.
        results3 = [
            dict(timesteps_this_iter=0, episodes_this_iter=0) for i in range(10)
        ]
        logs3, _ = self.checkAndReturnConsistentLogs(results3)

        self.assertTrue(all(log[TIMESTEPS_TOTAL] == 0 for log in logs3))
        self.assertTrue(all(log[EPISODES_TOTAL] == 0 for log in logs3))

        # Test that timesteps_total and episodes_total are properly counted
        # when timesteps_this_iter and episodes_this_iter report non-zero
        # values.
        results4 = [
            dict(timesteps_this_iter=3, episodes_this_iter=i) for i in range(10)
        ]
        logs4, _ = self.checkAndReturnConsistentLogs(results4)

        # The last reported result should not be double-logged.
        self.assertEqual(logs4[-1][TIMESTEPS_TOTAL], 30)
        self.assertNotEqual(logs4[-2][TIMESTEPS_TOTAL], logs4[-1][TIMESTEPS_TOTAL])
        self.assertEqual(logs4[-1][EPISODES_TOTAL], 45)
        self.assertNotEqual(logs4[-2][EPISODES_TOTAL], logs4[-1][EPISODES_TOTAL])

    def testAllValuesReceived(self):
        results1 = [
            dict(timesteps_total=(i + 1), my_score=i**2, done=i == 4)
            for i in range(5)
        ]

        logs1, _ = self.checkAndReturnConsistentLogs(results1)

        # check if the correct number of results were reported
        self.assertEqual(len(logs1), len(results1))

        def check_no_missing(reported_result, result):
            common_results = [reported_result[k] == result[k] for k in result]
            return all(common_results)

        # check that no result was dropped or modified
        complete_results = [
            check_no_missing(log, result) for log, result in zip(logs1, results1)
        ]
        self.assertTrue(all(complete_results))

        # check if done was logged exactly once
        self.assertEqual(len([r for r in logs1 if r.get("done")]), 1)

    def testNoDoneReceived(self):
        # repeat same test but without explicitly reporting done=True
        results1 = [dict(timesteps_total=(i + 1), my_score=i**2) for i in range(5)]

        logs1, trials = self.checkAndReturnConsistentLogs(results1)

        # check if the correct number of results were reported.
        self.assertEqual(len(logs1), len(results1))

        def check_no_missing(reported_result, result):
            common_results = [reported_result[k] == result[k] for k in result]
            return all(common_results)

        # check that no result was dropped or modified
        complete_results1 = [
            check_no_missing(log, result) for log, result in zip(logs1, results1)
        ]
        self.assertTrue(all(complete_results1))

    def _testDurableTrainable(self, trainable, function=False, cleanup=True):
        remote_checkpoint_dir = "memory:///unit-test/bucket"
        _ensure_directory(remote_checkpoint_dir)

        log_creator = partial(
            _noop_logger_creator, logdir="~/tmp/ray_results/exp/trial"
        )
        test_trainable = trainable(
            logger_creator=log_creator, remote_checkpoint_dir=remote_checkpoint_dir
        )
        result = test_trainable.train()
        self.assertEqual(result["metric"], 1)
        checkpoint_path = test_trainable.save()
        result = test_trainable.train()
        self.assertEqual(result["metric"], 2)
        result = test_trainable.train()
        self.assertEqual(result["metric"], 3)
        result = test_trainable.train()
        self.assertEqual(result["metric"], 4)

        shutil.rmtree("~/tmp/ray_results/exp/")
        if not function:
            test_trainable.state["hi"] = 2
            test_trainable.restore(checkpoint_path)
            self.assertEqual(test_trainable.state["hi"], 1)
        else:
            # Cannot re-use function trainable, create new
            tune.trainable.session._shutdown()
            test_trainable = trainable(
                logger_creator=log_creator,
                remote_checkpoint_dir=remote_checkpoint_dir,
            )
            test_trainable.restore(checkpoint_path)

        result = test_trainable.train()
        self.assertEqual(result["metric"], 2)

    def testDurableTrainableClass(self):
        class TestTrain(Trainable):
            def setup(self, config):
                self.state = {"hi": 1, "iter": 0}

            def step(self):
                self.state["iter"] += 1
                return {
                    "timesteps_this_iter": 1,
                    "metric": self.state["iter"],
                    "done": self.state["iter"] > 3,
                }

            def save_checkpoint(self, path):
                return self.state

            def load_checkpoint(self, state):
                self.state = state

        self._testDurableTrainable(TestTrain)

    def testDurableTrainableFunction(self):
        def test_train(config, checkpoint_dir=None):
            state = {"hi": 1, "iter": 0}
            if checkpoint_dir:
                with open(os.path.join(checkpoint_dir, "ckpt.pkl"), "rb") as fp:
                    state = pickle.load(fp)

            for i in range(4):
                state["iter"] += 1
                with tune.checkpoint_dir(step=state["iter"]) as dir:
                    with open(os.path.join(dir, "ckpt.pkl"), "wb") as fp:
                        pickle.dump(state, fp)
                tune.report(
                    **{
                        "timesteps_this_iter": 1,
                        "metric": state["iter"],
                        "done": state["iter"] > 3,
                    }
                )

        self._testDurableTrainable(wrap_function(test_train), function=True)

    def testDurableTrainableSyncFunction(self):
        """Check custom sync functions in durable trainables"""

        class CustomSyncer(Syncer):
            def sync_up(
                self, local_dir: str, remote_dir: str, exclude: list = None
            ) -> bool:
                pass  # sync up

            def sync_down(
                self, remote_dir: str, local_dir: str, exclude: list = None
            ) -> bool:
                pass  # sync down

            def delete(self, remote_dir: str) -> bool:
                pass  # delete

        class TestDurable(Trainable):
            def has_syncer(self):
                return isinstance(self.sync_config.syncer, Syncer)

        upload_dir = "s3://test-bucket/path"

        def _create_remote_actor(trainable_cls, sync_to_cloud):
            """Create a remote trainable actor from an experiment"""
            exp = Experiment(
                name="test_durable_sync",
                run=trainable_cls,
                sync_config=tune.SyncConfig(
                    syncer=sync_to_cloud, upload_dir=upload_dir
                ),
            )

            searchers = BasicVariantGenerator()
            searchers.add_configurations([exp])
            trial = searchers.next_trial()
            cls = trial.get_trainable_cls()
            actor = ray.remote(cls).remote(
                remote_checkpoint_dir=upload_dir,
                sync_config=trial.sync_config,
            )
            return actor

        # This actor should create a default syncer, so check should pass
        actor1 = _create_remote_actor(TestDurable, "auto")
        self.assertTrue(ray.get(actor1.has_syncer.remote()))

        # This actor should create a custom syncer, so check should pass
        actor2 = _create_remote_actor(TestDurable, CustomSyncer())
        self.assertTrue(ray.get(actor2.has_syncer.remote()))

    def testCheckpointDict(self):
        class TestTrain(Trainable):
            def setup(self, config):
                self.state = {"hi": 1}

            def step(self):
                return {"timesteps_this_iter": 1, "done": True}

            def save_checkpoint(self, path):
                return self.state

            def load_checkpoint(self, state):
                self.state = state

        test_trainable = TestTrain()
        result = test_trainable.save()
        test_trainable.state["hi"] = 2
        test_trainable.restore(result)
        self.assertEqual(test_trainable.state["hi"], 1)

        trials = run_experiments(
            {
                "foo": {
                    "run": TestTrain,
                    "checkpoint_config": CheckpointConfig(checkpoint_at_end=True),
                }
            }
        )
        for trial in trials:
            self.assertEqual(trial.status, Trial.TERMINATED)
            self.assertTrue(trial.has_checkpoint())

    def testMultipleCheckpoints(self):
        class TestTrain(Trainable):
            def setup(self, config):
                self.state = {"hi": 1, "iter": 0}

            def step(self):
                self.state["iter"] += 1
                return {"timesteps_this_iter": 1, "done": True}

            def save_checkpoint(self, path):
                return self.state

            def load_checkpoint(self, state):
                self.state = state

        test_trainable = TestTrain()
        checkpoint_1 = test_trainable.save()
        test_trainable.train()
        checkpoint_2 = test_trainable.save()
        self.assertNotEqual(checkpoint_1, checkpoint_2)
        test_trainable.restore(checkpoint_2)
        self.assertEqual(test_trainable.state["iter"], 1)
        test_trainable.restore(checkpoint_1)
        self.assertEqual(test_trainable.state["iter"], 0)

        trials = run_experiments(
            {
                "foo": {
                    "run": TestTrain,
                    "checkpoint_config": CheckpointConfig(checkpoint_at_end=True),
                }
            }
        )
        for trial in trials:
            self.assertEqual(trial.status, Trial.TERMINATED)
            self.assertTrue(trial.has_checkpoint())

    def testLogToFile(self):
        def train(config, reporter):
            import sys
            from ray import logger

            for i in range(10):
                reporter(timesteps_total=i)
            print("PRINT_STDOUT")
            print("PRINT_STDERR", file=sys.stderr)
            logger.info("LOG_STDERR")

        register_trainable("f1", train)

        # Do not log to file
        [trial] = tune.run("f1", log_to_file=False).trials
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stdout")))
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stderr")))

        # Log to default files
        [trial] = tune.run("f1", log_to_file=True).trials
        self.assertTrue(os.path.exists(os.path.join(trial.local_path, "stdout")))
        self.assertTrue(os.path.exists(os.path.join(trial.local_path, "stderr")))
        with open(os.path.join(trial.local_path, "stdout"), "rt") as fp:
            content = fp.read()
            self.assertIn("PRINT_STDOUT", content)
        with open(os.path.join(trial.local_path, "stderr"), "rt") as fp:
            content = fp.read()
            self.assertIn("PRINT_STDERR", content)
            self.assertIn("LOG_STDERR", content)

        # Log to one file
        [trial] = tune.run("f1", log_to_file="combined").trials
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stdout")))
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stderr")))
        self.assertTrue(os.path.exists(os.path.join(trial.local_path, "combined")))
        with open(os.path.join(trial.local_path, "combined"), "rt") as fp:
            content = fp.read()
            self.assertIn("PRINT_STDOUT", content)
            self.assertIn("PRINT_STDERR", content)
            self.assertIn("LOG_STDERR", content)

        # Log to two files
        [trial] = tune.run("f1", log_to_file=("alt.stdout", "alt.stderr")).trials
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stdout")))
        self.assertFalse(os.path.exists(os.path.join(trial.local_path, "stderr")))
        self.assertTrue(os.path.exists(os.path.join(trial.local_path, "alt.stdout")))
        self.assertTrue(os.path.exists(os.path.join(trial.local_path, "alt.stderr")))

        with open(os.path.join(trial.local_path, "alt.stdout"), "rt") as fp:
            content = fp.read()
            self.assertIn("PRINT_STDOUT", content)
        with open(os.path.join(trial.local_path, "alt.stderr"), "rt") as fp:
            content = fp.read()
            self.assertIn("PRINT_STDERR", content)
            self.assertIn("LOG_STDERR", content)

    def testTimeout(self):
        from ray.tune.stopper import TimeoutStopper
        import datetime

        def train(config):
            for i in range(20):
                tune.report(metric=i)
                time.sleep(1)

        register_trainable("f1", train)

        start = time.time()
        tune.run("f1", time_budget_s=5)
        diff = time.time() - start
        self.assertLess(diff, 10)

        # Metric should fire first
        start = time.time()
        tune.run("f1", stop={"metric": 3}, time_budget_s=7)
        diff = time.time() - start
        self.assertLess(diff, 7)

        # Timeout should fire first
        start = time.time()
        tune.run("f1", stop={"metric": 10}, time_budget_s=5)
        diff = time.time() - start
        self.assertLess(diff, 10)

        # Combined stopper. Shorter timeout should win.
        start = time.time()
        tune.run(
            "f1", stop=TimeoutStopper(10), time_budget_s=datetime.timedelta(seconds=3)
        )
        diff = time.time() - start
        self.assertLess(diff, 9)

    def testInfiniteTrials(self):
        def train(config):
            time.sleep(0.5)
            tune.report(np.random.uniform(-10.0, 10.0))

        start = time.time()
        out = tune.run(train, num_samples=-1, time_budget_s=10)
        taken = time.time() - start

        # Allow for init time overhead
        self.assertLessEqual(taken, 20.0)
        self.assertGreaterEqual(len(out.trials), 0)

        status = dict(Counter([trial.status for trial in out.trials]))
        self.assertGreaterEqual(status["TERMINATED"], 1)
        self.assertLessEqual(status.get("PENDING", 0), 1)

    def testMetricCheckingEndToEnd(self):
        def train(config):
            tune.report(val=4, second=8)

        def train2(config):
            return

        os.environ["TUNE_DISABLE_STRICT_METRIC_CHECKING"] = "0"
        # `acc` is not reported, should raise
        with self.assertRaises(TuneError):
            # The trial runner raises a ValueError, but the experiment fails
            # with a TuneError
            tune.run(train, metric="acc")

        # `val` is reported, should not raise
        tune.run(train, metric="val")

        # Run does not report anything, should not raise
        tune.run(train2, metric="val")

        # Only the scheduler requires a metric
        with self.assertRaises(TuneError):
            tune.run(train, scheduler=AsyncHyperBandScheduler(metric="acc", mode="max"))

        tune.run(train, scheduler=AsyncHyperBandScheduler(metric="val", mode="max"))

        # Only the search alg requires a metric
        with self.assertRaises(TuneError):
            tune.run(
                train,
                config={"a": tune.choice([1, 2])},
                search_alg=HyperOptSearch(metric="acc", mode="max"),
            )

        # Metric is passed
        tune.run(
            train,
            config={"a": tune.choice([1, 2])},
            search_alg=HyperOptSearch(metric="val", mode="max"),
        )

        os.environ["TUNE_DISABLE_STRICT_METRIC_CHECKING"] = "1"
        # With strict metric checking disabled, this should not raise
        tune.run(train, metric="acc")

    def testTrialDirCreation(self):
        def test_trial_dir(config):
            return 1.0

        # Per default, the directory should be named `test_trial_dir_{date}`
        with tempfile.TemporaryDirectory() as tmp_dir:
            tune.run(test_trial_dir, local_dir=tmp_dir)

            subdirs = list(os.listdir(tmp_dir))
            self.assertNotIn("test_trial_dir", subdirs)
            found = False
            for subdir in subdirs:
                if subdir.startswith("test_trial_dir_"):  # Date suffix
                    found = True
                    break
            self.assertTrue(found)

        # If we set an explicit name, no date should be appended
        with tempfile.TemporaryDirectory() as tmp_dir:
            tune.run(test_trial_dir, local_dir=tmp_dir, name="my_test_exp")

            subdirs = list(os.listdir(tmp_dir))
            self.assertIn("my_test_exp", subdirs)
            found = False
            for subdir in subdirs:
                if subdir.startswith("my_test_exp_"):  # Date suffix
                    found = True
                    break
            self.assertFalse(found)

        # Don't append date if we set the env variable
        os.environ["TUNE_DISABLE_DATED_SUBDIR"] = "1"
        with tempfile.TemporaryDirectory() as tmp_dir:
            tune.run(test_trial_dir, local_dir=tmp_dir)

            subdirs = list(os.listdir(tmp_dir))
            self.assertIn("test_trial_dir", subdirs)
            found = False
            for subdir in subdirs:
                if subdir.startswith("test_trial_dir_"):  # Date suffix
                    found = True
                    break
            self.assertFalse(found)

    def testWithParameters(self):
        class Data:
            def __init__(self):
                self.data = [0] * 500_000

        data = Data()
        data.data[100] = 1

        class TestTrainable(Trainable):
            def setup(self, config, data):
                self.data = data.data
                self.data[101] = 2  # Changes are local

            def step(self):
                return dict(metric=len(self.data), hundred=self.data[100], done=True)

        trial_1, trial_2 = tune.run(
            tune.with_parameters(TestTrainable, data=data), num_samples=2
        ).trials

        self.assertEqual(data.data[101], 0)
        self.assertEqual(trial_1.last_result["metric"], 500_000)
        self.assertEqual(trial_1.last_result["hundred"], 1)
        self.assertEqual(trial_2.last_result["metric"], 500_000)
        self.assertEqual(trial_2.last_result["hundred"], 1)
        self.assertTrue(str(trial_1).startswith("TestTrainable"))

    def testWithParameters2(self):
        class Data:
            def __init__(self):
                import numpy as np

                self.data = np.random.rand((2 * 1024 * 1024))

        class TestTrainable(Trainable):
            def setup(self, config, data):
                self.data = data.data

            def step(self):
                return dict(metric=len(self.data), done=True)

        trainable = tune.with_parameters(TestTrainable, data=Data())
        # ray.cloudpickle will crash for some reason
        import cloudpickle as cp

        dumped = cp.dumps(trainable)
        assert sys.getsizeof(dumped) < 100 * 1024

    def testWithParameters3(self):
        class Data:
            def __init__(self):
                import numpy as np

                self.data = np.random.rand((2 * 1024 * 1024))

        class TestTrainable(Trainable):
            def setup(self, config, data):
                self.data = data.data

            def step(self):
                return dict(metric=len(self.data), done=True)

        new_data = Data()
        ref = ray.put(new_data)
        trainable = tune.with_parameters(TestTrainable, data=ref)
        # ray.cloudpickle will crash for some reason
        import cloudpickle as cp

        dumped = cp.dumps(trainable)
        assert sys.getsizeof(dumped) < 100 * 1024


@pytest.fixture
def ray_start_2_cpus_2_gpus():
    address_info = ray.init(num_cpus=2, num_gpus=2)
    yield address_info
    # The code after the yield will run as teardown code.
    ray.shutdown()


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_dict(ray_start_2_cpus_2_gpus, num_gpus):
    def train_fn(config):
        return len(ray.get_gpu_ids())

    [trial] = tune.run(
        tune.with_resources(train_fn, resources={"gpu": num_gpus})
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_pgf(ray_start_2_cpus_2_gpus, num_gpus):
    def train_fn(config):
        return len(ray.get_gpu_ids())

    [trial] = tune.run(
        tune.with_resources(
            train_fn, resources=PlacementGroupFactory([{"GPU": num_gpus}])
        )
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_scaling_config(ray_start_2_cpus_2_gpus, num_gpus):
    def train_fn(config):
        return len(ray.get_gpu_ids())

    [trial] = tune.run(
        tune.with_resources(
            train_fn,
            resources=ScalingConfig(trainer_resources={"GPU": num_gpus}, num_workers=0),
        )
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_fn(ray_start_2_cpus_2_gpus, num_gpus):
    def train_fn(config):
        return len(ray.get_gpu_ids())

    [trial] = tune.run(
        tune.with_resources(
            train_fn,
            resources=lambda config: PlacementGroupFactory(
                [{"GPU": config["use_gpus"]}]
            ),
        ),
        config={"use_gpus": num_gpus},
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_class_fn(ray_start_2_cpus_2_gpus, num_gpus):
    class MyTrainable(tune.Trainable):
        def step(self):
            return {"_metric": len(ray.get_gpu_ids()), "done": True}

        def save_checkpoint(self, checkpoint_dir: str):
            pass

        def load_checkpoint(self, checkpoint):
            pass

        @classmethod
        def default_resource_request(cls, config):
            # This will be overwritten by tune.with_trainables()
            return PlacementGroupFactory([{"CPU": 2, "GPU": 0}])

    [trial] = tune.run(
        tune.with_resources(
            MyTrainable,
            resources=lambda config: PlacementGroupFactory(
                [{"GPU": config["use_gpus"]}]
            ),
        ),
        config={"use_gpus": num_gpus},
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_class_method(ray_start_2_cpus_2_gpus, num_gpus):
    class Worker:
        def train_fn(self, config):
            return len(ray.get_gpu_ids())

    worker = Worker()

    [trial] = tune.run(
        tune.with_resources(
            worker.train_fn,
            resources=lambda config: PlacementGroupFactory(
                [{"GPU": config["use_gpus"]}]
            ),
        ),
        config={"use_gpus": num_gpus},
    ).trials

    assert trial.last_result["_metric"] == num_gpus


@pytest.mark.parametrize("num_gpus", [1, 2])
def test_with_resources_and_parameters_fn(ray_start_2_cpus_2_gpus, num_gpus):
    def train_fn(config, extra_param=None):
        assert extra_param is not None, "Missing extra parameter."
        print(ray.get_runtime_context().get_assigned_resources())
        return {"num_gpus": len(ray.get_gpu_ids())}

    # Nesting `tune.with_parameters` and `tune.with_resources` should respect
    # the resource specifications.
    trainable = tune.with_resources(
        tune.with_parameters(train_fn, extra_param="extra"),
        {"gpu": num_gpus},
    )

    tuner = tune.Tuner(trainable)
    results = tuner.fit()
    print(results[0].metrics)
    assert results[0].metrics["num_gpus"] == num_gpus

    # The other order of nesting should work the same.
    trainable = tune.with_parameters(
        tune.with_resources(train_fn, {"gpu": num_gpus}), extra_param="extra"
    )
    tuner = tune.Tuner(trainable)
    results = tuner.fit()
    assert results[0].metrics["num_gpus"] == num_gpus


class SerializabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init(local_mode=True)

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()

    def tearDown(self):
        if "RAY_PICKLE_VERBOSE_DEBUG" in os.environ:
            del os.environ["RAY_PICKLE_VERBOSE_DEBUG"]

    def testNotRaisesNonserializable(self):
        import threading

        lock = threading.Lock()

        def train(config):
            print(lock)
            tune.report(val=4, second=8)

        with self.assertRaisesRegex(TypeError, "RAY_PICKLE_VERBOSE_DEBUG"):
            # The trial runner raises a ValueError, but the experiment fails
            # with a TuneError
            tune.run(train, metric="acc")

    def testRaisesNonserializable(self):
        os.environ["RAY_PICKLE_VERBOSE_DEBUG"] = "1"
        import threading

        lock = threading.Lock()

        def train(config):
            print(lock)
            tune.report(val=4, second=8)

        with self.assertRaises(TypeError) as cm:
            # The trial runner raises a ValueError, but the experiment fails
            # with a TuneError
            tune.run(train, metric="acc")
        msg = cm.exception.args[0]
        assert "RAY_PICKLE_VERBOSE_DEBUG" not in msg
        assert "thread.lock" in msg


class ShimCreationTest(unittest.TestCase):
    def testCreateScheduler(self):
        kwargs = {"metric": "metric_foo", "mode": "min"}

        scheduler = "async_hyperband"
        shim_scheduler = tune.create_scheduler(scheduler, **kwargs)
        real_scheduler = AsyncHyperBandScheduler(**kwargs)
        assert type(shim_scheduler) is type(real_scheduler)

    def testCreateLazyImportScheduler(self):
        kwargs = {
            "metric": "metric_foo",
            "mode": "min",
            "hyperparam_bounds": {"param1": [0, 1]},
        }
        shim_scheduler_pb2 = tune.create_scheduler("pb2", **kwargs)
        real_scheduler_pb2 = PB2(**kwargs)
        assert type(shim_scheduler_pb2) is type(real_scheduler_pb2)

    def testCreateSearcher(self):
        kwargs = {"metric": "metric_foo", "mode": "min"}

        searcher_ax = "ax"
        shim_searcher_ax = tune.create_searcher(searcher_ax, **kwargs)
        real_searcher_ax = AxSearch(space=[], **kwargs)
        assert type(shim_searcher_ax) is type(real_searcher_ax)

        searcher_hyperopt = "hyperopt"
        shim_searcher_hyperopt = tune.create_searcher(searcher_hyperopt, **kwargs)
        real_searcher_hyperopt = HyperOptSearch({}, **kwargs)
        assert type(shim_searcher_hyperopt) is type(real_searcher_hyperopt)

    def testExtraParams(self):
        kwargs = {"metric": "metric_foo", "mode": "min", "extra_param": "test"}

        scheduler = "async_hyperband"
        tune.create_scheduler(scheduler, **kwargs)

        searcher_ax = "ax"
        tune.create_searcher(searcher_ax, **kwargs)


class ApiTestFast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init(num_cpus=4, num_gpus=0, local_mode=True, include_dashboard=False)

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()
        _register_all()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def testNestedResults(self):
        def create_result(i):
            return {"test": {"1": {"2": {"3": i, "4": False}}}}

        flattened_keys = list(flatten_dict(create_result(0)))

        class _MockScheduler(FIFOScheduler):
            results = []

            def on_trial_result(self, trial_runner, trial, result):
                self.results += [result]
                return TrialScheduler.CONTINUE

            def on_trial_complete(self, trial_runner, trial, result):
                self.complete_result = result

        def train(config, reporter):
            for i in range(100):
                reporter(**create_result(i))

        algo = _MockSuggestionAlgorithm()
        scheduler = _MockScheduler()
        [trial] = tune.run(
            train, scheduler=scheduler, search_alg=algo, stop={"test/1/2/3": 20}
        ).trials
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result["test"]["1"]["2"]["3"], 20)
        self.assertEqual(trial.last_result["test"]["1"]["2"]["4"], False)
        self.assertEqual(trial.last_result[TRAINING_ITERATION], 21)
        self.assertEqual(len(scheduler.results), 20)
        self.assertTrue(
            all(set(result) >= set(flattened_keys) for result in scheduler.results)
        )
        self.assertTrue(set(scheduler.complete_result) >= set(flattened_keys))
        self.assertEqual(len(algo.results), 20)
        self.assertTrue(
            all(set(result) >= set(flattened_keys) for result in algo.results)
        )
        with self.assertRaises(TuneError):
            [trial] = tune.run(train, stop={"1/2/3": 20})
        with self.assertRaises(TuneError):
            [trial] = tune.run(train, stop={"test": 1}).trials

    def testIterationCounter(self):
        def train(config, reporter):
            for i in range(100):
                reporter(itr=i, timesteps_this_iter=1)

        register_trainable("exp", train)
        config = {
            "my_exp": {
                "run": "exp",
                "config": {
                    "iterations": 100,
                },
                "stop": {"timesteps_total": 100},
            }
        }
        [trial] = run_experiments(config)
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TRAINING_ITERATION], 100)
        self.assertEqual(trial.last_result["itr"], 99)

    def testErrorReturn(self):
        def train(config, reporter):
            raise Exception("uh oh")

        register_trainable("f1", train)

        def f():
            run_experiments(
                {
                    "foo": {
                        "run": "f1",
                    }
                }
            )

        self.assertRaises(TuneError, f)

    def testSuccess(self):
        def train(config, reporter):
            for i in range(100):
                reporter(timesteps_total=i)

        register_trainable("f1", train)
        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                }
            }
        )
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result[TIMESTEPS_TOTAL], 99)

    def testNoRaiseFlag(self):
        def train(config, reporter):
            raise Exception()

        register_trainable("f1", train)

        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                }
            },
            raise_on_failed_trial=False,
        )
        self.assertEqual(trial.status, Trial.ERROR)

    def testReportInfinity(self):
        def train(config, reporter):
            for _ in range(100):
                reporter(mean_accuracy=float("inf"))

        register_trainable("f1", train)
        [trial] = run_experiments(
            {
                "foo": {
                    "run": "f1",
                }
            }
        )
        self.assertEqual(trial.status, Trial.TERMINATED)
        self.assertEqual(trial.last_result["mean_accuracy"], float("inf"))

    def testSearcherSchedulerStr(self):
        capture = {}

        class MockTrialRunner(TrialRunner):
            def __init__(self, search_alg=None, scheduler=None, **kwargs):
                # should be converted from strings at this case and not None
                capture["search_alg"] = search_alg
                capture["scheduler"] = scheduler
                super().__init__(
                    search_alg=search_alg,
                    scheduler=scheduler,
                    **kwargs,
                )

        with patch("ray.tune.tune.TrialRunner", MockTrialRunner):
            tune.run(
                lambda config: tune.report(metric=1),
                search_alg="random",
                scheduler="async_hyperband",
                metric="metric",
                mode="max",
                stop={TRAINING_ITERATION: 1},
            )

        self.assertIsInstance(capture["search_alg"], BasicVariantGenerator)
        self.assertIsInstance(capture["scheduler"], AsyncHyperBandScheduler)


class MaxConcurrentTrialsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init(num_cpus=4, num_gpus=0, local_mode=False, include_dashboard=False)

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()
        _register_all()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def testMaxConcurrentTrials(self):
        def train(config):
            tune.report(metric=1)

        capture = {}

        class MockTrialRunner(TrialRunner):
            def __init__(self, search_alg=None, scheduler=None, **kwargs):
                # should be converted from strings at this case and not None
                capture["search_alg"] = search_alg
                capture["scheduler"] = scheduler
                super().__init__(
                    search_alg=search_alg,
                    scheduler=scheduler,
                    **kwargs,
                )

        with patch("ray.tune.tune.TrialRunner", MockTrialRunner):
            tune.run(
                train,
                config={"a": tune.randint(0, 2)},
                metric="metric",
                mode="max",
                stop={TRAINING_ITERATION: 1},
            )

            self.assertIsInstance(capture["search_alg"], BasicVariantGenerator)
            self.assertEqual(capture["search_alg"].max_concurrent, 0)

            tune.run(
                train,
                max_concurrent_trials=2,
                config={"a": tune.randint(0, 2)},
                metric="metric",
                mode="max",
                stop={TRAINING_ITERATION: 1},
            )

            self.assertIsInstance(capture["search_alg"], BasicVariantGenerator)
            self.assertEqual(capture["search_alg"].max_concurrent, 2)

            tune.run(
                train,
                search_alg=HyperOptSearch(),
                config={"a": tune.randint(0, 2)},
                metric="metric",
                mode="max",
                stop={TRAINING_ITERATION: 1},
            )

            self.assertIsInstance(capture["search_alg"].searcher, HyperOptSearch)

            tune.run(
                train,
                search_alg=HyperOptSearch(),
                max_concurrent_trials=2,
                config={"a": tune.randint(0, 2)},
                metric="metric",
                mode="max",
                stop={TRAINING_ITERATION: 1},
            )

            self.assertIsInstance(capture["search_alg"].searcher, ConcurrencyLimiter)
            self.assertEqual(capture["search_alg"].searcher.max_concurrent, 2)

            # max_concurrent_trials should not override ConcurrencyLimiter
            with self.assertRaisesRegex(ValueError, "max_concurrent_trials"):
                tune.run(
                    train,
                    search_alg=ConcurrencyLimiter(HyperOptSearch(), max_concurrent=3),
                    max_concurrent_trials=2,
                    config={"a": tune.randint(0, 2)},
                    metric="metric",
                    mode="max",
                    stop={TRAINING_ITERATION: 1},
                )


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
