import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jetson_tests.common import digest, save_json
from jetson_tests.config import PASSES, config_hash, load_config, matrix, validate
from jetson_tests.metrics import cyclictest, perf, process_pass
from jetson_tests.pmu import inventory, pass_events
from jetson_tests.report import generate
from jetson_tests.worker import acquire

ROOT = Path(__file__).resolve().parents[1]
SELECTED = {"victim_instructions": ["instructions:u", "instructions:k"]}


class PerfConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        events = self.out / "armv8_cortex_a78/events"
        events.mkdir(parents=True)
        (events / "inst_retired").write_text("event=0x0008")
        self.catalog = inventory(self.out)

    def test_defaults_and_three_fast_cases(self):
        self.assertEqual(matrix(validate({}))[0]["passes"], PASSES)
        self.assertFalse(any(name.startswith("task_") for name in PASSES))
        for name in PASSES:
            self.assertTrue(pass_events(name, self.catalog, include_unavailable=True))
        self.assertEqual(config_hash(validate({})), digest(validate({})))
        configs = [load_config(ROOT / "campaign" / name)[0] for name in (
            "campaign-instructions-base.yaml", "campaign-instructions-isolcpu0.yaml")]
        rows = [(c, row) for c in configs for row in matrix(c)]
        self.assertEqual([(r["scenario"], c["isolcpu"]) for c, r in rows],
                         [("interfgen", []), ("demo", []), ("demo", [0])])
        self.assertEqual(sum(len(r["passes"]) * r["duration_s"] for _, r in rows), 90)
        for _, row in rows:
            self.assertEqual(row["passes"], ["victim_instructions"])
            self.assertEqual(row["kind"], "measurement")
        c = validate({"perf_passes": {**SELECTED, "victim_cycles": ["cycles"]}})
        self.assertEqual(matrix(c)[0]["passes"], ["victim_instructions", "victim_cycles"])
        reordered = {**c, "perf_passes": dict(reversed(list(c["perf_passes"].items())))}
        self.assertNotEqual(config_hash(c), config_hash(reordered))
        self.assertEqual(config_hash(c), config_hash(json.loads(json.dumps(c))))

    def test_invalid_passes_and_events(self):
        for passes in ({}, [], None, {"../escape": ["instructions"]},
                       {"profiling": ["cycles"]}, {"victim_": ["cycles"]},
                       {"victim_x": []}, {"victim_x": "instructions"},
                       {"victim_x": ["cycles", "cycles"]}, {"victim_x": [{}]},
                       {"victim_x": ["instructions:x"]}, {"victim_x": ["instructions:uk"]},
                       {"victim_x": ["task_clock:u"]}, {"victim_x": ["invented"]}):
            with self.subTest(passes=passes), self.assertRaises(ValueError):
                validate({"perf_passes": passes})
        with self.assertRaisesRegex(ValueError, "replace the task_ prefix with victim_"):
            validate({"perf_passes": {"task_instructions": ["instructions"]}})

    def test_documented_example_is_a_valid_fast_campaign(self):
        c, _ = load_config(ROOT / "campaign/example.yaml")
        self.assertEqual(c["perf_passes"], SELECTED)
        self.assertEqual([(r["scenario"], r["passes"]) for r in matrix(c)],
                         [("interfgen", ["victim_instructions"]), ("demo", ["victim_instructions"])])
        self.assertEqual(set(c["scenarios"]["demo"]["definition"]), {
            "name", "environment_script", "resources", "warmup_s", "startup_timeout_s",
            "shutdown_timeout_s", "foreign_process_names", "processes"})

    def test_modifiers_availability_and_csv_with_or_without_cgroup(self):
        expected = {"instructions:u": "armv8_cortex_a78/inst_retired/u",
                    "instructions:k": "armv8_cortex_a78/inst_retired/k"}
        self.assertEqual(pass_events("victim_instructions", self.catalog, configured=SELECTED), expected)
        missing = copy.deepcopy(self.catalog)
        missing["instructions"]["available"] = False
        self.assertEqual(pass_events("victim_instructions", missing, configured=SELECTED), {})
        self.assertEqual(pass_events("victim_instructions", missing, True, SELECTED), expected)
        for group in ("", "jetson-campaign-test,"):
            path = self.out / "perf.csv"
            path.write_text("".join(f"{100000 + i},,{event},{group}1000,100.00\n"
                                    for i, event in enumerate(expected.values())))
            counts = perf(path, expected)
            self.assertEqual([r["value"] for r in counts.values()], [100000, 100001])

    def test_cache_misses_are_default_and_branches_are_selectable(self):
        for level, miss in (("l1d", "l1d_long_miss_reads"),
                            ("l1i", "l1i_long_misses"),
                            ("l2", "l2_long_miss_reads"),
                            ("l3", "l3_long_miss_reads")):
            for scope in (level, "victim_" + level):
                self.assertIn(miss, pass_events(scope, self.catalog, include_unavailable=True))
        branch = {"victim_branches": ["branch_predictions", "branch_mispredictions",
                                      "branches_retired", "branch_mispredictions_retired:k"]}
        c = validate({"perf_passes": branch})
        selected = pass_events("victim_branches", self.catalog, True, c["perf_passes"])
        self.assertEqual(selected["branch_mispredictions_retired:k"],
                         "armv8_cortex_a78/br_mis_pred_retired/k")
        self.assertEqual(len(selected), 4)

    def test_jetson_branch_and_miss_encodings(self):
        encodings = {"br_pred": "0x0012", "br_mis_pred": "0x0010",
                     "br_retired": "0x0021", "br_mis_pred_retired": "0x0022",
                     "l1d_cache_lmiss_rd": "0x0039", "l1i_cache_lmiss": "0x4006",
                     "l2d_cache_lmiss_rd": "0x4009", "l3d_cache_lmiss_rd": "0x400b"}
        for event, encoding in encodings.items():
            (self.out / "armv8_cortex_a78/events" / event).write_text("event=" + encoding)
        found = inventory(self.out)
        for key in ("branch_predictions", "branch_mispredictions", "branches_retired",
                    "branch_mispredictions_retired", "l1d_long_miss_reads", "l1i_long_misses",
                    "l2_long_miss_reads", "l3_long_miss_reads"):
            self.assertTrue(found[key]["available"], key)

    def test_worker_uses_selected_events_for_task_and_cgroup(self):
        c = validate({"perf_passes": SELECTED, "profiling": {"enabled": False}})
        item = matrix(c)[0]
        for group in (None, "jetson-campaign-test"):
            manager = Mock()
            def spawn(name, argv, **kwargs):
                if name == "cyclictest":
                    raise RuntimeError("stop before launching workload")
                return {"record": {"name": name}}
            manager.spawn.side_effect = spawn
            with patch("jetson_tests.worker.snapshot"), self.assertRaisesRegex(RuntimeError, "stop before"):
                acquire(c, item, "victim_instructions", self.catalog, manager, Mock(), self.out, [], group, [4])
            calls = manager.spawn.call_args_list
            measured = calls[0].args[1]
            self.assertIn("armv8_cortex_a78/inst_retired/u", measured)
            self.assertIn("armv8_cortex_a78/inst_retired/k", measured)
            self.assertEqual(measured.count("-e"), 2)
            if group:
                self.assertEqual(len(calls), 2)
                self.assertEqual(measured[measured.index("-G") + 1], group)
                self.assertEqual(calls[1].args[1][0], "sudo")
            else:
                self.assertEqual(len(calls), 1)
                self.assertNotIn("-a", measured[:measured.index("--")])
                self.assertIn("cyclictest", measured)

    def test_report_and_coverage_with_only_split_instructions(self):
        c = validate({"perf_passes": SELECTED, "profiling": {"enabled": False}})
        relative = "core0/baseline/measurement_001/attempt_001"
        directory = self.out / "results" / relative / "pass_victim_instructions"
        directory.mkdir(parents=True)
        expected = pass_events("victim_instructions", self.catalog, configured=SELECTED)
        save_json(directory / "measurement.json", {"scopes": {"victim_task": expected}})
        (directory / "cyclictest.json").write_bytes((ROOT / "tests/fixtures/cyclictest.json").read_bytes())
        path = directory / "perf_victim_task.csv"
        path.write_text("".join(f"200000,,{e},1000,100.00\n" for e in expected.values()))
        measured = process_pass(directory, 0, 90)
        self.assertNotIn("victim_task instructions do not cover cyclictest samples", measured["issues"])
        save_json(directory.parent / "metrics.json", {"passes": [measured]})
        save_json(self.out / "config.json", c)
        save_json(self.out / "campaign.json", {"config_hash": "test", "attempts": [
            {"item": matrix(c)[0], "relative": relative, "outcome": "PASS", "download_verified": True}]})
        report = generate(self.out)
        with (report / "runs.csv").open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["victim_task__pass_victim_instructions__instructions:u"], "200000")
        self.assertEqual(row["victim_task__pass_victim_instructions__instructions:k"], "200000")
        self.assertEqual(int(row["samples"]), cyclictest(directory / "cyclictest.json", 0)["samples"])
        self.assertTrue(json.loads((report / "aggregates.json").read_text()))
        self.assertNotIn("victim_cpu__pass_core__cycles", row)
        path.write_text("".join(f"100,,{e},1000,100.00\n" for e in expected.values()))
        self.assertIn("victim_task instructions do not cover cyclictest samples", process_pass(directory, 0, 90)["issues"])


if __name__ == "__main__":
    unittest.main()
