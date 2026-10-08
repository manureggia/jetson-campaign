"""Run these on Linux/Jetson: real owned subprocesses, no PMU or RT privilege needed."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from jetson_tests.processes import Processes, RunFailure, matches
from jetson_tests.config import validate
from jetson_tests.hardware import shell
from jetson_tests.worker import readiness, start_interferers, prepare_resources, workload_command


class ProcessEnvironmentTests(unittest.TestCase):
    def test_perf_locale_overrides_inherited_and_explicit_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Processes(directory, time.monotonic() + 30)
            identity = {"pid": 123, "start": "10"}
            config = validate({"victim_cores": [0], "perf_cpus": [4, 5]})
            wrapped_perf, _, composite = workload_command(
                config, 0, 1, Path(directory), "victim_core", ["-e", "task-clock"], True,
                perf_cpus=[4, 5])
            self.assertTrue(composite)
            for command, supplied, expected in (
                (["perf", "stat"], None, {"LC_ALL": "C", "CUSTOM_TEST": "kept"}),
                (wrapped_perf, None, {"LC_ALL": "C", "CUSTOM_TEST": "kept"}),
                (["/usr/local/bin/perf", "stat"], {"LC_ALL": "it_IT.UTF-8", "ONLY": "yes"},
                 {"LC_ALL": "C", "ONLY": "yes"}),
                (["cyclictest"], None, None),
                (["bash", "-c", "true"], {"LC_ALL": "it_IT.UTF-8"}, {"LC_ALL": "it_IT.UTF-8"}),
            ):
                with self.subTest(command=command, supplied=supplied), \
                     patch.dict(os.environ, {"LC_ALL": "it_IT.UTF-8", "CUSTOM_TEST": "kept"}, clear=True), \
                     patch("jetson_tests.processes.subprocess.Popen", return_value=Mock(pid=123)) as spawn, \
                     patch("jetson_tests.processes.process_info", return_value=identity), \
                     patch("jetson_tests.processes.boot_id", return_value="test-boot"):
                    original = supplied.copy() if supplied is not None else None
                    manager.spawn("probe", command, env=supplied, cpus=[4, 5])
                    self.assertEqual(spawn.call_args.kwargs["env"], expected)
                    self.assertEqual(spawn.call_args.args[0][:3], ["taskset", "-c", "4,5"])
                    self.assertEqual(supplied, original)
                    self.assertEqual(os.environ["LC_ALL"], "it_IT.UTF-8")


@unittest.skipUnless(sys.platform == "linux", "Linux process identity and sched affinity")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.manager = Processes(self.out, time.monotonic() + 30, .2)
        self.addCleanup(self.manager.cleanup)
        self.cpu = min(os.sched_getaffinity(0))

    def test_shell_quotes_environment_and_affinity(self):
        env = self.out / "environment.sh"
        env.write_text("export CUSTOM_TEST='hello world'\n")
        job = self.manager.spawn("quoted", shell('printf "%s" "$CUSTOM_TEST"; sleep 1', str(env)), cpus=[self.cpu])
        self.manager.sleep(.2)
        self.manager.refresh()
        self.manager.check_affinity()
        self.assertEqual(self.manager.wait(job), 0)
        self.assertEqual(Path(job["record"]["stdout"]).read_text(), "hello world")

    def test_cleanup_descendants_keeps_foreign_process(self):
        import subprocess
        foreign = subprocess.Popen(["sleep", "20"], start_new_session=True)
        self.addCleanup(lambda: foreign.wait())
        self.addCleanup(foreign.terminate)
        job = self.manager.spawn("parent", shell('sleep 20 & wait'), cpus=[self.cpu], required=True)
        self.manager.sleep(.3)
        self.manager.refresh()
        self.assertGreaterEqual(len(job["known"]), 2)
        self.manager.stop(job)
        self.assertFalse(self.manager.live_members(job))
        self.assertIsNone(foreign.poll())

    def test_hard_timeout_cleans_up(self):
        job = self.manager.spawn("timeout", ["sleep", "10"])
        with self.assertRaises(RunFailure):
            self.manager.wait(job, .2)
        self.assertIsNotNone(job["proc"].poll())

    def test_orphaned_child_after_leader_exit(self):
        job = self.manager.spawn("orphan", shell("sleep 20 & exit 0"))
        time.sleep(.3)
        job["proc"].poll()
        self.manager.refresh()
        self.assertTrue(self.manager.live_members(job))
        self.manager.stop(job)
        self.assertFalse(self.manager.live_members(job))

    def test_required_crash(self):
        self.manager.spawn("crash", shell("exit 7"), required=True)
        time.sleep(.2)
        with self.assertRaises(RunFailure):
            self.manager.check()

    def test_readiness_regex_and_failure(self):
        profile = {"startup_timeout_s": 2}
        job = self.manager.spawn("ready", shell('echo READY; sleep 10'), required=True)
        readiness(self.manager, job, {"name": "ready", "ready_log_regex": "READY"}, profile, self.out)
        profile["startup_timeout_s"] = .2
        with self.assertRaises(RunFailure):
            readiness(self.manager, job, {"name": "ready", "ready_log_regex": "NEVER"}, profile, self.out)

    def test_profile_swap_and_repeated_player(self):
        profile = {"startup_timeout_s": 2, "processes": [
            {"name": "arbitrary", "command": "echo initialized; sleep 10", "ready_log_regex": "initialized"},
            {"name": "play", "command": "echo FRAME; sleep .1", "repeat_on_success": True, "ready_log_regex": "FRAME"}]}
        c = {"scenarios": {"demo": {"definition": profile}}}
        jobs = start_interferers(c, {"scenario": "demo"}, self.manager, self.out, self.out, [self.cpu])
        self.manager.sleep(.4)
        self.assertEqual(len(jobs), 2)
        self.assertGreater(Path(jobs[-1]["record"]["stdout"]).read_text().count("FRAME"), 1)

    def test_resource_copy_and_missing_input(self):
        source = self.out / "input.txt"
        source.write_text("immutable data")
        work = self.out / "workspace"
        manifest = prepare_resources({"resources": {"data/in.txt": str(source)}}, work)
        self.assertIn("data/in.txt", manifest)
        self.assertEqual((work / "data/in.txt").read_text(), "immutable data")
        with self.assertRaises(OSError):
            prepare_resources({"resources": {"missing": str(self.out / 'missing')}}, self.out / "bad")


if __name__ == "__main__":
    unittest.main()
