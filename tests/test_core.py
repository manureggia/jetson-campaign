import copy
from contextlib import redirect_stdout
import errno
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from jetson_tests.common import RunState, PHASES, choose_attempt, save_json
from jetson_tests.cli import perf_conflict_request
from jetson_tests.config import load_config, validate, matrix, placement
from jetson_tests.metrics import cyclictest, perf, interrupts, histogram_summary, number, process_pass
from jetson_tests.pmu import inventory
from jetson_tests.report import aggregate, selected_attempts, generate
from jetson_tests.transport import verify_download
from jetson_tests.worker import cgroup_exec, cgroup_name, cyclic_command, workload_command

ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_real_cyclictest_fixture(self):
        row = cyclictest(ROOT / "tests/fixtures/cyclictest.json", 0)
        self.assertEqual(row["samples"], 30000)
        self.assertTrue(row["histogram_complete"])
        self.assertGreater(row["std_us"], 0)
        self.assertEqual(row["max_us"], 53)
        with self.assertRaises(ValueError):
            cyclictest(ROOT / "tests/fixtures/cyclictest.json", 3)

    def test_nanosecond_cyclictest_fixture(self):
        # Real `cyclictest -N` output from the Jetson: buckets and summary in ns.
        row = cyclictest(ROOT / "tests/fixtures/cyclictest_ns.json", 3)
        self.assertEqual(row["samples"], 5000)
        self.assertTrue(row["histogram_complete"])
        self.assertEqual(row["resolution_ns"], 1)
        self.assertAlmostEqual(row["min_us"], 2.085)
        self.assertAlmostEqual(row["max_us"], 4.749)
        self.assertAlmostEqual(row["reported_mean_us"], 2.61641)
        self.assertAlmostEqual(row["mean_us"], row["reported_mean_us"], places=2)
        self.assertTrue(2.085 <= row["p50_us"] <= row["p99_us"] <= 4.749)

    def test_cyclictest_command_uses_nanoseconds(self):
        c = validate({"victim_cores": [3], "cyclictest": {"histogram_us": 1000}})
        argv = cyclic_command(c, 3, 60, self.out)
        self.assertIn("-N", argv)
        self.assertEqual(argv[argv.index("-h") + 1], "1000000")
        self.assertIn("--histfile=/dev/null", argv)

    def test_histogram_overflow(self):
        data = json.loads((ROOT / "tests/fixtures/cyclictest.json").read_text())
        data["thread"]["0"]["cycles"] += 100  # 100 samples above the histogram range
        save_json(self.out / "ct.json", data)
        result = cyclictest(self.out / "ct.json", 0)
        self.assertIsNone(result["std_us"])
        self.assertEqual(result["overflow_samples"], 100)
        self.assertEqual(result["max_us"], 53)
        # Rank 29799 of 30100 is inside the histogram, rank 30070 falls among the overflows.
        self.assertIsNotNone(result["p99_us"])
        self.assertIsNone(result["p99_9_us"])

    def pass_directory(self, name, softirq_sched=(0, 0), cgroup=None, extra_cycles=0):
        directory = self.out / name
        directory.mkdir()
        save_json(directory / "measurement.json", {"scopes": {}, "cgroup": cgroup,
                  "cyclictest_start_monotonic": 100.0, "cyclictest_end_monotonic": 130.0})
        data = json.loads((ROOT / "tests/fixtures/cyclictest.json").read_text())
        data["thread"]["0"]["cycles"] += extra_cycles
        save_json(directory / "cyclictest.json", data)
        for suffix, value in (("before", 10), ("after", 20)):
            (directory / f"interrupts_{suffix}.txt").write_text(f" CPU0 CPU3\n 13: {value} {value} GIC timer\n")
        for suffix, sched in zip(("before", "after"), softirq_sched):
            (directory / f"softirqs_{suffix}.txt").write_text(
                f"   CPU0 CPU3\n HI: 0 0\n SCHED: {sched} 7\n HRTIMER: 5 5\n")
        return directory

    def test_overflow_is_a_warning_not_a_failure(self):
        result = process_pass(self.pass_directory("pass_core", extra_cycles=1), 0, 90)
        self.assertEqual(result["issues"], [])
        self.assertTrue(any("Histogram overflow" in w for w in result["warnings"]))

    def test_sched_softirq_burst_is_flagged_only_on_isolated_victim(self):
        quiet = process_pass(self.pass_directory("pass_core", (100, 110), "jetson-campaign-0"), 0, 90)
        self.assertEqual(quiet["softirqs"]["victim"]["SCHED"], 10)
        self.assertFalse(any("SCHED softirq" in w for w in quiet["warnings"]))
        burst = process_pass(self.pass_directory("pass_cache_l1", (100, 6100), "jetson-campaign-0"), 0, 90)
        self.assertTrue(any("SCHED softirq burst on isolated CPU0: 6000 (200/s)" in w for w in burst["warnings"]))
        self.assertEqual(burst["issues"], [])
        shared = process_pass(self.pass_directory("pass_cache_l2_l3", (100, 6100)), 0, 90)
        self.assertFalse(any("SCHED softirq" in w for w in shared["warnings"]))
        self.assertTrue((self.out / "pass_core/softirqs_delta.csv").exists())

    def test_victim_pass_latency_is_marked_perturbed(self):
        self.assertEqual(process_pass(self.pass_directory("pass_core"), 0, 90)["latency_role"], "reference")
        self.assertEqual(process_pass(self.pass_directory("pass_victim_core"), 0, 90)["latency_role"],
                         "perturbed_by_task_counters")

    def test_perf_exact_event_and_large_integer(self):
        path = self.out / "perf.csv"
        path.write_text('9007199254740993,,arm/l1d_cache/,1000,100.0\n<not counted>,,arm/l1d_cache_refill/,0,0\n')
        result = perf(path, {"access": "arm/l1d_cache/", "refill": "arm/l1d_cache_refill/"})
        self.assertEqual(result["access"]["value"], 9007199254740993)
        self.assertIsNone(result["refill"]["value"])
        self.assertEqual(number("9007199254740993"), 9007199254740993)
        self.assertIsNone(number("nan"))

    def test_real_perf_fixture_and_scaling(self):
        row = perf(ROOT / "tests/fixtures/perf_victim_cpu.csv",
                   {"cycles": "armv8_cortex_a78/cpu_cycles/", "task_clock": "task-clock"})
        self.assertEqual(row["cycles"]["value"], 1267927528)
        self.assertEqual(row["task_clock"]["value"], 30000.14)
        self.assertEqual(row["task_clock"]["unit"], "msec")
        p = self.out / "scaled.csv"
        p.write_text('100,,cycles,1000,50\n')
        self.assertIsNone(perf(p, {"cycles": "cycles"})["cycles"]["value"])
        p.write_text('65028350,,instructions,jetson-campaign-9d57a832719a4e50,22144320,100.00\n')
        counted = perf(p, {"instructions": "instructions"})["instructions"]
        self.assertEqual(counted["value"], 65028350)
        self.assertEqual(counted["time_running_ns"], 22144320)

    def test_task_counters_must_cover_cyclictest(self):
        directory = self.out / "pass_victim_core"
        directory.mkdir()
        (directory / "measurement.json").write_text('{"scopes":{"victim_task":{"instructions":"instructions"}}}')
        (directory / "cyclictest.json").write_bytes((ROOT / "tests/fixtures/cyclictest.json").read_bytes())
        (directory / "perf_victim_task.csv").write_text('4110,,instructions,11680,100.00\n')
        result = process_pass(directory, 0, 90)
        self.assertIn("victim_task instructions do not cover cyclictest samples", result["issues"])

    def test_interrupt_delta_and_invalid_snapshot(self):
        a, b = self.out / "before", self.out / "after"
        a.write_text(' CPU0 CPU3\n 1: 10 20 GIC timer\n IPI0: 1 2 rescheduling\n')
        b.write_text(' CPU0 CPU3\n 1: 15 27 GIC timer\n IPI0: 2 4 rescheduling\n')
        result = interrupts(a, b, 3, self.out / "delta.csv")
        self.assertEqual(result["victim"], 9)
        self.assertEqual(result["total"], 15)
        b.write_text(' CPU0 CPU3\n 1: 1 2 GIC timer\n')
        self.assertIsNone(interrupts(a, b, 3, self.out / "delta.csv")["total"])
        b.write_text(' CPU0\n 1: 11 GIC timer\n')
        self.assertTrue(interrupts(a, b, 0, self.out / "delta.csv")["issues"])

    def test_config_matrix_placement_and_changed_demo(self):
        c, sources = load_config(ROOT / "campaign/campaign.yaml")
        items = matrix(c)
        self.assertEqual(len(items), 24)
        self.assertEqual(len([i for i in items if i["kind"] == "measurement"]), 18)
        self.assertEqual(len(items[0]["passes"]), 12)
        topology = {"online": list(range(6)), "clusters": {"0": [0, 1, 2, 3], "3": [0, 1, 2, 3]}}
        self.assertEqual(placement({"placement": "same_cluster"}, 0, topology), [1, 2, 3])
        self.assertEqual(placement({"placement": "system"}, 3, topology), [0, 1, 2, 4, 5])
        with self.assertRaises(ValueError):
            placement({"placement": "explicit", "cpus": [0]}, 0, topology)
        c["scenarios"]["demo"]["definition"]["processes"] = [{"name": "anything", "command": "printf 'hello world'"}]
        self.assertEqual(len(validate(c)["scenarios"]["demo"]["definition"]["processes"]), 1)
        c["cyclictest"]["extra_args"] = ["-a3"]
        with self.assertRaises(ValueError):
            validate(c)

    def test_invalid_profiles(self):
        c, _ = load_config(ROOT / "campaign/campaign.yaml")
        c["scenarios"]["demo"]["definition"]["resources"] = {"../escape": "/tmp/file"}
        with self.assertRaises(ValueError):
            validate(c)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            validate({"repetitons": 3})
        c["scenarios"]["demo"]["definition"]["resources"] = {"dir": "/tmp/dir", "dir/file": "/tmp/file"}
        with self.assertRaises(ValueError):
            validate(c)

    def test_isolcpu_perf_cpus_and_hooks(self):
        c = validate({"victim_cores": [0, 3], "isolcpu": 0, "perf_cpus": [4, 5],
                      "command-pre-test": ["jetson_clocks --fan"],
                      "command-post-test": ["jetson_clocks --restore saved.conf"]})
        self.assertEqual(c["isolcpu"], [0])
        self.assertEqual(c["perf_cpus"], [4, 5])
        with self.assertRaisesRegex(ValueError, "subset"):
            validate({"victim_cores": [0], "isolcpu": [0, 3]})
        with self.assertRaisesRegex(ValueError, "unique"):
            validate({"victim_cores": [0], "perf_cpus": [4, 4]})
        with self.assertRaisesRegex(ValueError, "list"):
            validate({"command-pre-test": "jetson_clocks --fan"})
        name = cgroup_name("core0/baseline/measurement_001/attempt_001")
        argv = cgroup_exec(name, ["taskset", "-c", "0", "cyclictest"], 90)
        self.assertEqual(argv[3:], ["exec", "--rtprio", "90", name, "--",
                                   "taskset", "-c", "0", "cyclictest"])
        normal, cpus, composite = workload_command(c, 0, 10, self.out, "core", [], False)
        self.assertEqual(normal[0], "cyclictest")
        self.assertEqual(cpus, [0])
        self.assertFalse(composite)
        isolated, cpus, _ = workload_command(c, 0, 10, self.out, "core", [], False, name, [4, 5])
        self.assertEqual(isolated[:8], ["sudo", "-n", "/usr/local/sbin/jetson-campaign-cgroup",
                                       "exec", "--rtprio", "90", name, "--"])
        self.assertEqual(isolated[8:12], ["taskset", "-c", "0", "cyclictest"])
        self.assertIsNone(cpus)
        measured, cpus, composite = workload_command(c, 0, 10, self.out, "victim_core",
                                                      ["-e", "cycles"], True, name, [4, 5])
        self.assertEqual(measured[:3], ["sudo", "-n", "/usr/local/sbin/jetson-campaign-cgroup"])
        self.assertIn("/usr/local/sbin/jetson-campaign-cgroup", measured)
        self.assertIsNone(cpus)
        self.assertFalse(composite)
        conflict = perf_conflict_request(
            validate({"victim_cores": [0], "perf_cpus": [0], "profiling": {"enabled": False}}),
            {"topology": {"online": [0, 1, 2]}, "placements": {}})
        self.assertEqual(conflict["conflicts"][0]["free"], [1, 2])
        self.assertEqual(conflict["options"], ["fallback", "collision", "stop"])

    def test_cgroup_probe_creates_configured_partition(self):
        path = ROOT / "tools/jetson-campaign-cgroup"
        loader = SourceFileLoader("jetson_campaign_cgroup", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        (self.out / "cgroup.controllers").write_text("cpu cpuset\n")
        (self.out / "cpuset.cpus.effective").write_text("0-3\n")
        helper.ROOT = self.out
        name = "jetson-campaign-0000000000000001"
        target = self.out / name
        target.mkdir()
        (target / "cpuset.cpus.partition").write_text("isolated\n")
        with patch.object(helper.os, "getpid", return_value=1), \
             patch.object(helper, "create", return_value="isolated") as create, \
             patch.object(helper, "delete") as delete, \
             patch.object(helper, "set_rtprio") as set_rtprio, \
             redirect_stdout(io.StringIO()):
            helper.probe(90)
        create.assert_called_once_with(name, 0)
        delete.assert_called_once_with(name)
        set_rtprio.assert_called_once_with(90)

    def test_cgroup_helper_bounds_realtime_limit(self):
        path = ROOT / "tools/jetson-campaign-cgroup"
        loader = SourceFileLoader("jetson_campaign_cgroup_rtprio", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        with patch.object(helper.resource, "setrlimit") as setrlimit:
            helper.set_rtprio(90)
        setrlimit.assert_called_once_with(helper.RLIMIT_RTPRIO, (90, 90))
        with self.assertRaisesRegex(ValueError, "between 0 and 99"):
            helper.set_rtprio(100)

    def test_cgroup_create_falls_back_to_root_partition_on_linux_5_15(self):
        path = ROOT / "tools/jetson-campaign-cgroup"
        loader = SourceFileLoader("jetson_campaign_cgroup_fallback", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        (self.out / "cpuset.cpus.effective").write_text("0-3\n")
        (self.out / "cpuset.mems.effective").write_text("0\n")
        helper.ROOT = self.out
        real_write = helper.write

        def linux_5_15_write(target, value):
            if target.name == "cpuset.cpus.partition" and value == "isolated":
                raise OSError(errno.EINVAL, "Invalid argument")
            real_write(target, value)

        with patch.object(helper, "write", side_effect=linux_5_15_write):
            state = helper.create("jetson-campaign-0000000000000001", 0)
        self.assertEqual(state, "root")

    def test_state_resume_and_force_generation(self):
        state = RunState(self.out)
        with self.assertRaises(ValueError):
            state.transition("RUNNING")
        for phase in PHASES[1:-1]:
            state.transition(phase)
        state.transition("CLEANUP")
        state.transition("COMPLETE", outcome="PASS")
        self.assertEqual(RunState(self.out).value["outcome"], "PASS")
        self.assertEqual(choose_attempt([{"outcome": "PASS"}]), "skip")
        self.assertEqual(choose_attempt([{"outcome": "FAIL"}], rerun_failed=True), "run")
        item = {"core": 0, "scenario": "baseline", "kind": "measurement", "run": 1}
        attempts = [{"item": item, "outcome": "PASS", "download_verified": True, "generation": 0},
                    {"item": item, "outcome": "FAIL", "download_verified": True, "generation": 1}]
        self.assertEqual(selected_attempts(attempts)[0]["outcome"], "FAIL")

    def test_weighted_statistics(self):
        common = dict(core=0, scenario="baseline", placement="none", status="PASS", kind="measurement")
        rows = [common | histogram_summary({"1": 1}) | {"min_us": 1, "max_us": 1},
                common | histogram_summary({"3": 3}) | {"min_us": 3, "max_us": 3}]
        result = aggregate(rows)[0]
        self.assertEqual(result["pooled_mean_us"], 2.5)
        self.assertAlmostEqual(result["pooled_std_us"], 3**.5 / 2)
        self.assertIsNone(aggregate(rows[:1])[0]["between_run_std_us"])

    def test_manifest_tampering(self):
        from jetson_tests.common import file_hash
        path = self.out / "raw.txt"
        path.write_text("raw")
        save_json(self.out / "manifest.json", {"raw.txt": file_hash(path)})
        verify_download(self.out)
        path.write_text("changed")
        with self.assertRaises(ValueError):
            verify_download(self.out)

    def test_pmu_encoding_mismatch(self):
        path = self.out / "armv8_cortex_a78/events"
        path.mkdir(parents=True)
        (path / "l1d_cache_refill").write_text("event=0x17")
        self.assertFalse(inventory(self.out)["l1d_refills"]["available"])

    def test_report_from_historical_latency_fixture(self):
        relative = "core0/baseline/measurement_001/attempt_001"
        item = {"core": 0, "scenario": "baseline", "kind": "measurement", "run": 1}
        save_json(self.out / "config.json", validate({}))
        save_json(self.out / "campaign.json", {"config_hash": "historical-fixture-only", "attempts": [
            {"item": item, "relative": relative, "outcome": "PASS", "download_verified": True}]})
        summary = cyclictest(ROOT / "tests/fixtures/cyclictest.json", 0)
        save_json(self.out / "results" / relative / "metrics.json", {"passes": [
            {"pass": "pass_core", "latency": summary, "interrupts": {"total": None, "victim": None}, "perf": {}}]})
        report = generate(self.out)
        ET.parse(report / "latency.svg")
        aggregate_rows = json.loads((report / "aggregates.json").read_text())
        self.assertAlmostEqual(aggregate_rows[0]["pooled_mean_us"], summary["mean_us"])
        self.assertIn("victim_task__pass_victim_l3__l3_refills", (report / "runs.csv").read_text())
        self.assertIn("victim_task__pass_victim_l3__l3_long_miss_reads", (report / "runs.csv").read_text())
        self.assertIn("cyclictest.json", (report / "provenance.csv").read_text())


if __name__ == "__main__":
    unittest.main()
