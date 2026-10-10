"""Regression coverage for disappearing tasks and discarded perf windows."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from jetson_tests.common import RunState, read_json
from jetson_tests.config import validate
from jetson_tests.processes import Processes, RunFailure, thread_ids
from jetson_tests.worker import acquire, acquire_once, check_monitors


ESRCH = ("Error:\nThe sys_perf_event_open() syscall returned with 3 (No such process) "
         "for event (armv8_cortex_a78/cpu_cycles/).\n")


class MonitorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.manager = Processes(self.out, time.monotonic() + 30)

    def job(self, name="perf_interferer", code=255, stderr=ESRCH):
        path = self.out / f"{name}.stderr.txt"
        path.write_text(stderr)
        return {"proc": Mock(poll=Mock(return_value=code)), "logged": False,
                "record": {"id": "0001_" + name, "name": name, "stderr": str(path),
                           "start_monotonic": time.monotonic()}}

    def state(self):
        state = RunState(self.out)
        for phase in ("PRECHECK", "SETUP", "WARMUP"):
            state.transition(phase)
        return state

    def test_zero_tid_is_filtered_for_both_affinity_and_fifo_inspection(self):
        paths = [Path("/proc/123/task/0"), Path("/proc/123/task/123")]
        with patch("jetson_tests.processes.Path.glob", return_value=paths):
            self.assertEqual(list(thread_ids(123)), [123])
            self.manager.jobs = [{"cpus": [1], "known": {123: {}}, "record": {"name": "pose"}}]
            with patch("jetson_tests.processes.matches", return_value=True), \
                 patch("jetson_tests.processes.os.sched_getaffinity", return_value={1}, create=True) as affinity, \
                 patch("jetson_tests.processes.os.sched_getscheduler", return_value=0, create=True), \
                 patch("jetson_tests.processes.os.sched_getparam", return_value=Mock(sched_priority=0), create=True):
                self.manager.check_affinity()
                affinity.assert_called_once_with(123)

    def test_disappearing_thread_is_skipped_but_real_affinity_violation_fails(self):
        self.manager.jobs = [{"cpus": [1], "known": {123: {}}, "record": {"name": "pose"}}]
        with patch("jetson_tests.processes.matches", return_value=True), \
             patch("jetson_tests.processes.thread_ids", return_value=[123, 124]), \
             patch("jetson_tests.processes.os.sched_getaffinity", side_effect=[ProcessLookupError(), {2}], create=True), \
             patch("jetson_tests.processes.os.sched_getscheduler", return_value=0, create=True), \
             patch("jetson_tests.processes.os.sched_getparam", return_value=Mock(sched_priority=0), create=True):
            with self.assertRaises(RunFailure) as failure:
                self.manager.check_affinity()
        self.assertEqual(failure.exception.category, "affinity")
        self.assertIn("'tid': 124", str(failure.exception))

    def test_monitor_failure_preserves_exit_code_and_stderr(self):
        with self.assertRaises(RunFailure) as failure:
            check_monitors(self.manager, [self.job()])
        self.assertEqual(failure.exception.category, "perf_attach")
        row = json.loads((self.out / "monitor_failures.jsonl").read_text())
        self.assertEqual(row["exit_code"], 255)
        self.assertEqual(row["stderr_tail"], ESRCH)
        self.assertEqual(row["command_id"], "0001_perf_interferer")

    def test_normal_exit_and_running_monitors_are_accepted(self):
        check_monitors(self.manager, [self.job(code=0), self.job("perf_victim_cpu", code=None)])
        self.assertFalse((self.out / "monitor_failures.jsonl").exists())

    def test_only_interferer_esrch_is_retryable(self):
        for name, stderr in (("perf_interferer", "Permission denied"),
                             ("perf_interferer", "No such process"),
                             ("perf_victim_cpu", ESRCH)):
            with self.subTest(name=name, stderr=stderr), self.assertRaises(RunFailure) as failure:
                check_monitors(self.manager, [self.job(name, stderr=stderr)])
            self.assertEqual(failure.exception.category, "exit_code")

    def test_failed_monitor_interrupts_acquisition_before_waiting_for_cyclictest(self):
        manager = Mock(directory=self.out)
        victim = self.job("perf_victim_cpu", code=None)
        interferer = self.job()
        cyclic = {"proc": Mock(poll=Mock(return_value=None)), "known": {},
                  "record": {"start_monotonic": time.monotonic()}}
        manager.spawn.side_effect = [victim, interferer, cyclic]
        manager.live_members.return_value = [{"pid": 123}]
        manager.finish_log.side_effect = lambda job: job["proc"].poll()
        c = validate({"perf_passes": {"core": ["task_clock"]}})
        catalog = {"task_clock": {"event": "task-clock", "available": True}}
        with patch("jetson_tests.worker.snapshot"), \
             patch("jetson_tests.worker.time.sleep") as sleep, self.assertRaises(RunFailure) as failure:
            acquire_once(c, {"core": 0, "duration_s": 180}, "core", catalog, manager,
                         self.state(), self.out, [{"cpus": [2, 1]}, {"cpus": [1]}])
        self.assertEqual(failure.exception.category, "perf_attach")
        manager.wait.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(read_json(self.out / "interferer_attachment.json")["cpus"], [1, 2])
        interferer_argv = manager.spawn.call_args_list[1].args[1]
        self.assertEqual(interferer_argv[4:7], ["-a", "-C", "1,2"])
        self.assertNotIn("-p", interferer_argv)

    def test_pid_attachment_excludes_zombies(self):
        self.manager.jobs = []
        job = {"known": {123: {"pid": 123, "start": "1"}, 124: {"pid": 124, "start": "2"}}}
        with patch("jetson_tests.processes.process_info", side_effect=[
                {"pid": 123, "start": "1", "state": "Z"},
                {"pid": 124, "start": "2", "state": "S"}]):
            self.assertEqual(self.manager.live_members(job), [{"pid": 124, "start": "2", "state": "S"}])

    def retry_run(self, failures):
        manager = Mock(jobs=["interferer"])
        state = self.state()
        output = self.out / "pass_core"
        output.mkdir()
        calls = []

        def run(c, item, pass_name, catalog, manager, state, target, *args):
            calls.append(target)
            if len(calls) > 1:
                previous = ["perf_cpu_1", "perf_interferer_1", "cyclictest_1"]
                for job in previous:
                    self.assertIn(unittest.mock.call(job), manager.stop.call_args_list)
            for phase in ("SNAPSHOT_BEFORE", "START_MONITORS", "START_WORKLOADS", "RUNNING"):
                state.transition(phase)
            manager.jobs.extend([f"perf_cpu_{len(calls)}", f"perf_interferer_{len(calls)}", f"cyclictest_{len(calls)}"])
            (target / "perf_interferer.csv").write_text("discarded" if len(calls) <= len(failures) else "valid")
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            for phase in ("STOP_WORKLOADS", "STOP_MONITORS", "SNAPSHOT_AFTER", "PROCESS_RESULTS", "VALIDATE"):
                state.transition(phase)
            return {"pass": target.name, "window": {}, "issues": []}

        with patch("jetson_tests.worker.acquire_once", side_effect=run):
            try:
                metrics = acquire({}, {"core": 0}, "core", {}, manager, state, output, ["interferer"])
            except RunFailure:
                metrics = None
        manager.stop.assert_any_call("cyclictest_1")
        self.assertNotIn(unittest.mock.call("interferer"), manager.stop.call_args_list)
        return metrics, output, calls

    def test_retry_stops_entire_window_and_only_publishes_success(self):
        metrics, output, calls = self.retry_run([RunFailure(ESRCH, "perf_attach")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(metrics["pass"], "pass_core")
        self.assertEqual(metrics["window"]["acquisition_attempt"], 2)
        self.assertEqual((output / "perf_interferer.csv").read_text(), "valid")
        self.assertEqual((calls[0] / "perf_interferer.csv").read_text(), "discarded")
        self.assertEqual(read_json(calls[0] / "failure.json")["category"], "perf_attach")
        self.assertEqual(read_json(output / "metrics.json"), metrics)

    def test_retry_budget_is_bounded_and_exhaustion_publishes_no_metrics(self):
        metrics, output, calls = self.retry_run([RunFailure(ESRCH, "perf_attach")] * 3)
        self.assertIsNone(metrics)
        self.assertEqual(len(calls), 3)
        self.assertFalse((output / "metrics.json").exists())
        self.assertFalse((output / "perf_interferer.csv").exists())

    def test_other_failure_does_not_retry(self):
        metrics, output, calls = self.retry_run([RunFailure("Permission denied", "exit_code")])
        self.assertIsNone(metrics)
        self.assertEqual(len(calls), 1)
        self.assertFalse((output / "metrics.json").exists())


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("JETSON_TEST_ROOT"),
                     "Dedicated Jetson workspace and perf/RT permissions required")
class LinuxMonitorRecoveryTests(unittest.TestCase):
    def test_real_acquisition_recovers_esrch_while_threads_change(self):
        with tempfile.TemporaryDirectory(dir=os.environ["JETSON_TEST_ROOT"]) as temporary:
            root = Path(temporary)
            cpus = sorted(os.sched_getaffinity(0))
            self.assertGreaterEqual(len(cpus), 2)
            victim, interferer_cpu = cpus[-2:]
            manager = Processes(root, time.monotonic() + 20, .2)
            churn = ("import threading,time; end=time.monotonic()+15\n"
                     "while time.monotonic()<end:\n"
                     " t=threading.Thread(target=time.sleep,args=(.02,)); t.start(); t.join()\n")
            interferer = manager.spawn("synthetic", [sys.executable, "-c", churn],
                                       cpus=[interferer_cpu], required=True)
            try:
                manager.sleep(.2)
                original_spawn = manager.spawn
                injected = False

                def spawn(name, argv, **kwargs):
                    nonlocal injected
                    if name == "perf_interferer" and not injected:
                        injected = True
                        argv = [sys.executable, "-c", "import sys; sys.stderr.write(" + repr(ESRCH) + "); sys.exit(255)"]
                    return original_spawn(name, argv, **kwargs)

                state = RunState(root)
                for phase in ("PRECHECK", "SETUP", "WARMUP"):
                    state.transition(phase)
                c = validate({"victim_cores": [victim], "duration_s": 1,
                              "perf_cpus": [interferer_cpu], "perf_passes": {"core": ["task_clock"]}})
                target = root / "pass_core"
                target.mkdir()
                with patch.object(manager, "spawn", side_effect=spawn):
                    metrics = acquire(c, {"core": victim, "duration_s": 1}, "core",
                                      {"task_clock": {"event": "task-clock", "available": True}},
                                      manager, state, target, [interferer], perf_cpus=[interferer_cpu])
                self.assertEqual(metrics["issues"], [])
                self.assertGreaterEqual(metrics["window"]["acquisition_attempt"], 2)
                self.assertEqual(metrics["latency"]["samples"], 1000)
                self.assertEqual(metrics["perf"]["interferer"]["task_clock"]["status"], "ok")
                self.assertEqual(metrics["pass"], "pass_core")
                self.assertFalse((target / "acquisition_001/metrics.json").exists())
                self.assertEqual(read_json(target / "acquisition_001/failure.json")["category"], "perf_attach")
                self.assertIsNone(interferer["proc"].poll())
                measurements = [j for j in manager.jobs if j["record"]["name"] == "cyclictest"]
                self.assertEqual(len(measurements), metrics["window"]["acquisition_attempt"])
                self.assertTrue(all(j["proc"].poll() is not None for j in measurements))
            finally:
                manager.cleanup()
                evidence = Path(os.environ["JETSON_TEST_ROOT"]) / "validation-results" / root.name
                evidence.parent.mkdir(exist_ok=True)
                shutil.copytree(root, evidence)


if __name__ == "__main__":
    unittest.main()
