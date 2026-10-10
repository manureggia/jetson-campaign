"""IRQ anomalies remain reviewable without dropping valid PMU/latency data."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from jetson_tests.cli import main
from jetson_tests.common import read_json, save_json
from jetson_tests.config import config_hash, matrix, validate
from jetson_tests.hardware import doctor
from jetson_tests.metrics import process_pass
from jetson_tests.report import generate
from jetson_tests.transport import verify_download
from jetson_tests.worker import execute, snapshot


FIXTURES = Path(__file__).resolve().parent / "fixtures"
BEFORE = ' CPU0 CPU3\n 1: 10 20 GIC timer\n 242: 219 0 interrupt-controller 32 Level\n'
AFTER = ' CPU0 CPU3\n 1: 15 27 GIC timer\n 242: 333 0 interrupt-controller 32 Level\n'


class MeasurementWarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.config = validate({"victim_cores": [0], "duration_s": 30,
                                "perf_passes": {"core": ["task_clock"]},
                                "profiling": {"enabled": False}})
        self.relative = "core0/baseline/measurement_001/attempt_001"
        self.run = self.out / "results" / self.relative
        self.measurement = self.run / "pass_core"
        self.measurement.mkdir(parents=True)
        save_json(self.measurement / "measurement.json", {"scopes": {"victim_cpu": {"task_clock": "task-clock"}}})
        shutil.copy2(FIXTURES / "cyclictest.json", self.measurement / "cyclictest.json")
        shutil.copy2(FIXTURES / "perf_victim_cpu.csv", self.measurement / "perf_victim_cpu.csv")
        (self.measurement / "interrupts_before.txt").write_text(BEFORE)
        (self.measurement / "interrupts_after.txt").write_text(AFTER)
        for suffix in ("before", "after"):  # The worker always snapshots both files.
            (self.measurement / f"softirqs_{suffix}.txt").write_text("   CPU0 CPU3\n SCHED: 4 0\n")

    def measured(self, renamed=False):
        if renamed:
            (self.measurement / "interrupts_after.txt").write_text(AFTER.replace('32 Level', '32 Level dma1chan0'))
        return process_pass(self.measurement, 0, 90)

    def campaign(self, metrics):
        save_json(self.out / "config.json", self.config)
        save_json(self.out / "campaign.json", {"config_hash": config_hash(self.config), "attempts": [
            {"item": matrix(self.config)[0], "relative": self.relative, "outcome": "PASS", "download_verified": True}]})
        save_json(self.run / "metrics.json", {"passes": [metrics]})

    def test_irq_changes_missing_rows_resets_and_headers_are_warnings(self):
        cases = [AFTER.replace('32 Level', '32 Level dma1chan0'),
                 AFTER.replace('333 0', '1 0'),
                 ' CPU0 CPU3\n 1: 15 27 GIC timer\n',
                 ' CPU0\n 1: 15 GIC timer\n', None]
        for after in cases:
            with self.subTest(after=after):
                path = self.measurement / "interrupts_after.txt"
                if after is None:
                    path.unlink()
                else:
                    path.write_text(after)
                metrics = self.measured()
                self.assertEqual(metrics["issues"], [])
                self.assertTrue(metrics["warnings"])
                self.assertIsNone(metrics["interrupts"]["total"])
                self.assertEqual(metrics["latency"]["samples"], 30000)
                self.assertTrue(metrics["latency"]["histogram_complete"])
                self.assertEqual(metrics["perf"]["victim_cpu"]["task_clock"]["status"], "ok")

    def test_perf_and_latency_errors_remain_blocking(self):
        (self.measurement / "perf_victim_cpu.csv").write_text('<not counted>,,task-clock,0,0\n')
        metrics = self.measured(renamed=True)
        self.assertIn("victim_cpu/task_clock: <not counted>", metrics["issues"])
        self.assertTrue(metrics["warnings"])
        shutil.copy2(FIXTURES / "perf_victim_cpu.csv", self.measurement / "perf_victim_cpu.csv")
        (self.measurement / "cyclictest.json").unlink()
        metrics = self.measured()
        self.assertIsNone(metrics["latency"])
        self.assertTrue(metrics["issues"])

    def test_report_preserves_warned_data_and_exports_review_context(self):
        metrics = self.measured(renamed=True)
        self.campaign(metrics)
        report = generate(self.out)
        row = read_json(report / "runs.json")[0]["run"]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["samples"], 30000)
        self.assertIsNotNone(row["victim_cpu__pass_core__task_clock"])
        self.assertIsNone(row["interrupts_total"])
        self.assertEqual(read_json(report / "aggregates.json")[0]["runs"], 1)
        warnings = read_json(report / "warnings.json")
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0]["attempt"], self.relative)
        self.assertEqual(warnings[0]["pass"], "pass_core")
        self.assertTrue((self.out / warnings[0]["metrics"]).exists())
        self.assertIn("IRQ 242", (report / "warnings.csv").read_text())
        self.assertIn("Avvisi non bloccanti", (report / "summary.md").read_text())

    def test_report_without_warnings_stays_compatible(self):
        metrics = self.measured()
        metrics.pop("warnings")  # Older campaign results did not include this field.
        self.campaign(metrics)
        report = generate(self.out)
        self.assertEqual(read_json(report / "warnings.json"), [])
        self.assertNotIn("Avvisi non bloccanti", (report / "summary.md").read_text())

    def test_cli_announces_warnings_only_after_acquisition_finishes(self):
        self.campaign(self.measured(renamed=True))
        output = io.StringIO()

        def finish(out, config, args):
            generate(out)
            self.assertNotIn("Avvisi non bloccanti", output.getvalue())

        with patch("jetson_tests.cli.run_campaign", side_effect=finish), redirect_stdout(output):
            code = main(["resume", "--campaign", str(self.out)])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertEqual(text.count("Avvisi non bloccanti:"), 1)
        self.assertLess(text.index("Report:"), text.index("Avvisi non bloccanti:"))


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("JETSON_TEST_ROOT"),
                     "Dedicated Jetson workspace and perf/RT permissions required")
class LinuxMeasurementWarningTests(unittest.TestCase):
    def test_irq_warning_does_not_skip_l1d_or_l2(self):
        with tempfile.TemporaryDirectory(dir=os.environ["JETSON_TEST_ROOT"]) as temporary:
            root = Path(temporary)
            cpus = sorted(os.sched_getaffinity(0))
            self.assertGreaterEqual(len(cpus), 2)
            # Leave time for the one-second process inventory to observe the FIFO thread.
            config = validate({"victim_cores": [cpus[-2]], "perf_cpus": [cpus[-1]], "duration_s": 3,
                               "cooldown_s": 0, "cyclictest": {"histogram_us": 10000},
                               "scenarios": {"baseline": {"warmup_s": 0}}, "profiling": {"enabled": False},
                               "perf_passes": {"victim_memory": ["cycles", "memory_accesses", "bus_accesses"],
                                               "l1d": ["cycles", "l1d_accesses", "l1d_refills"],
                                               "l2": ["cycles", "l2_accesses", "l2_refills"]}})
            save_json(root / "config.json", config)
            relative = f"core{cpus[-2]}/baseline/measurement_001/attempt_001"
            item = matrix(config)[0]
            request = {"item": item, "relative": relative}
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
            timer = signal.getitimer(signal.ITIMER_REAL)

            def renamed_snapshot(directory, suffix):
                snapshot(directory, suffix)
                if directory.name == "pass_victim_memory" and suffix == "after":
                    path = directory / "interrupts_after.txt"
                    lines = path.read_text().splitlines()
                    index = next(i for i, line in enumerate(lines) if line.strip().partition(":")[0].isdigit())
                    lines[index] += " codex-test-irq-label"
                    path.write_text("\n".join(lines) + "\n")

            try:
                info = doctor(config, root / "doctor")
                self.assertTrue(info["ok"], info["errors"])
                with patch("jetson_tests.worker.snapshot", side_effect=renamed_snapshot):
                    self.assertEqual(execute(root, request), 0)
                out = root / "results" / relative
                verify_download(out)
                self.assertEqual(read_json(out / "status.json")["outcome"], "PASS")
                passes = read_json(out / "metrics.json")["passes"]
                self.assertEqual([p["pass"] for p in passes], ["pass_victim_memory", "pass_l1d", "pass_l2"])
                self.assertTrue(passes[0]["warnings"])
                self.assertIsNone(passes[0]["interrupts"]["total"])
                self.assertTrue(all(not p["issues"] and p["latency"]["samples"] == 3000 for p in passes))
                self.assertTrue(all(v["status"] == "ok" for p in passes for scope in p["perf"].values() for v in scope.values()))
                save_json(root / "campaign.json", {"config_hash": config_hash(config), "attempts": [
                    {"item": item, "relative": relative, "outcome": "PASS", "download_verified": True}]})
                self.assertTrue(read_json(generate(root) / "warnings.json"))
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                signal.setitimer(signal.ITIMER_REAL, *timer)
                evidence = Path(os.environ["JETSON_TEST_ROOT"]) / "validation-results" / root.name
                evidence.parent.mkdir(exist_ok=True)
                shutil.copytree(root, evidence)


if __name__ == "__main__":
    unittest.main()
