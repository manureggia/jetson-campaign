"""Acquisition order, resume, and sequential CLI execution without hardware."""
import argparse
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jetson_tests.cli import campaign_matrix, create_campaign, main, run_campaign
from jetson_tests.common import read_json, save_json
from jetson_tests.config import config_hash, matrix, validate
from tests.test_controller import FakeTransport


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.config = validate({
            "victim_cores": [0, 3], "repetitions": 5,
            "randomization": {"enabled": True, "seed": 424242},
            "profiling": {"enabled": True},
            "scenarios": {
                "baseline": {}, "interfgen": {"command": "meminterf"},
                "demo": {"definition": {"processes": [{"name": "demo", "command": "demo"}]}}
            },
        })

    def test_seeded_blocks_preserve_conditions_passes_and_profiling(self):
        rows = matrix(self.config)
        measurements = [row for row in rows if row["kind"] == "measurement"]
        expected = {(cpu, scenario) for cpu in [0, 3] for scenario in ["baseline", "interfgen", "demo"]}
        for run in range(1, 6):
            block = measurements[(run - 1) * 6:run * 6]
            self.assertEqual({row["run"] for row in block}, {run})
            self.assertEqual({(row["core"], row["scenario"]) for row in block}, expected)
        self.assertTrue(all(row["kind"] == "profiling" for row in rows[30:]))
        self.assertEqual(len(rows[30:]), 6)
        self.assertEqual(rows, matrix(json.loads(json.dumps(self.config))))
        legacy = copy.deepcopy(self.config)
        legacy.pop("randomization")
        key = lambda row: (row["core"], row["scenario"], row["kind"], row["run"])
        self.assertEqual(sorted(rows, key=key), sorted(matrix(legacy), key=key))
        changed = copy.deepcopy(self.config)
        changed["randomization"]["seed"] += 1
        self.assertNotEqual(rows, matrix(changed))
        self.assertNotEqual(config_hash(self.config), config_hash(changed))

    def test_filters_keep_the_full_randomized_order(self):
        rows = matrix(self.config)
        self.assertEqual(matrix(self.config, core=0), [row for row in rows if row["core"] == 0])
        self.assertEqual(matrix(self.config, scenario="demo"), [row for row in rows if row["scenario"] == "demo"])
        with self.assertRaisesRegex(ValueError, "no experiments"):
            matrix(self.config, core=8)

    def test_omitted_or_disabled_randomization_keeps_legacy_order(self):
        c = validate({"victim_cores": [0, 3], "repetitions": 2, "profiling": {"enabled": False}})
        self.assertNotIn("randomization", c)
        self.assertEqual([(row["core"], row["run"]) for row in matrix(c)],
                         [(0, 1), (0, 2), (3, 1), (3, 2)])
        disabled = validate({**c, "randomization": {"enabled": False}})
        self.assertEqual(matrix(c), matrix(disabled))

    def test_invalid_randomization_settings(self):
        for settings in (True, [], {"enabled": "true"}, {"enabled": True},
                         {"enabled": True, "seed": True}, {"seed": -1},
                         {"seed": 1.5}, {"seed": None}, {"enabled": True, "seed": 1, "unknown": 0}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate({"randomization": settings})
        self.assertTrue(validate({"randomization": {"enabled": True, "seed": 0}}))

    def test_saved_order_and_resume_skip_completed_without_reshuffling(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            c = copy.deepcopy(self.config)
            c["profiling"]["enabled"] = False
            out = create_campaign(c, {"campaign.yaml": "name: test"}, base / "campaign")
            order = read_json(out / "order.json")["experiments"]
            transport = FakeTransport(base / "remote")
            transport.remote.mkdir()
            args = argparse.Namespace(core=0, scenario=None, force=False, rerun_failed=False)
            with patch("jetson_tests.cli.time.sleep"), redirect_stdout(io.StringIO()):
                run_campaign(out, c, args, transport)
                args.core = None
                run_campaign(out, c, args, transport)
            self.assertEqual([request["item"] for request in transport.launches],
                             [row for row in order if row["core"] == 0]
                             + [row for row in order if row["core"] == 3])
            self.assertEqual(read_json(out / "order.json")["experiments"], order)
            # The persisted order takes precedence over a regenerated permutation.
            with patch("jetson_tests.cli.matrix", return_value=list(reversed(order))):
                self.assertEqual(campaign_matrix(out, c), order)
            saved = read_json(out / "order.json")
            saved["experiments"].pop()
            save_json(out / "order.json", saved)
            with self.assertRaisesRegex(ValueError, "Saved acquisition order"):
                campaign_matrix(out, c)


class CampaignSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sequence_count = 0
        self.paths = []
        for index in range(2):
            path = self.base / f"campaign-{index}.yaml"
            path.write_text(f"name: sequence_{index}\nvictim_cores: [0]\nrepetitions: 1\n"
                            "duration_s: 1\nprofiling:\n  enabled: false\n")
            self.paths.append(path)

    def invoke(self, command, *extra):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            rc = main([command, "--config", *(str(path) for path in self.paths), *extra])
        return rc, output.getvalue()

    def test_plan_is_offline_and_reports_campaigns_in_argument_order(self):
        with patch("jetson_tests.cli.reserve_remote_dir") as reserve:
            rc, output = self.invoke("plan")
        self.assertEqual(rc, 0)
        reserve.assert_not_called()
        plan = json.loads(output)
        self.assertEqual([c["name"] for c in plan["campaigns"]], ["sequence_0", "sequence_1"])
        self.assertEqual(plan["execution"], "sequential")
        self.assertEqual(plan["acquisition_seconds"], 24)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["plan", "--config", str(self.paths[0])]), 0)
        self.assertIn("experiments", json.loads(output.getvalue()))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["plan", "--config", str(self.paths[0]),
                                   "--config", str(self.paths[1])]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["campaigns"]), 2)

    def run_sequence(self, outcome="PASS", missing=False, raises=False):
        events = []
        self.sequence_count += 1
        def create(c, sources, destination, reserve):
            events.append(("create", c["name"]))
            return create_campaign(c, sources, self.base / f"run-{self.sequence_count}" / c["name"])
        def run(out, c, args):
            events.append(("run", c["name"]))
            if raises:
                raise raises
            ledger = read_json(out / "campaign.json")
            ledger["attempts"] = [] if missing else [
                {"item": item, "outcome": outcome, "download_verified": True}
                for item in matrix(c)
            ]
            save_json(out / "campaign.json", ledger)
        with patch("jetson_tests.cli.create_campaign", side_effect=create), \
                patch("jetson_tests.cli.run_campaign", side_effect=run):
            rc, output = self.invoke("run")
        return rc, events

    def test_next_campaign_starts_only_after_the_previous_completes(self):
        rc, events = self.run_sequence()
        self.assertEqual(rc, 0)
        self.assertEqual(events, [(operation, f"sequence_{index}")
                                  for index in range(2) for operation in ["create", "run"]])

    def test_failure_incomplete_or_missing_results_stop_the_sequence(self):
        for outcome, missing, raises in [("FAIL", False, False), ("INCOMPLETE", False, False),
                                        ("PASS", True, False), ("PASS", False, RuntimeError("acquisition stopped"))]:
            with self.subTest(outcome=outcome, missing=missing, raises=raises):
                rc, events = self.run_sequence(outcome, missing, raises)
                self.assertEqual(rc, 2)
                self.assertEqual(events, [("create", "sequence_0"), ("run", "sequence_0")])

    def test_interrupt_does_not_start_the_next_campaign(self):
        rc, events = self.run_sequence(raises=KeyboardInterrupt())
        self.assertEqual(rc, 130)
        self.assertEqual(events, [("create", "sequence_0"), ("run", "sequence_0")])

    def test_invalid_second_yaml_is_rejected_before_first_campaign_is_created(self):
        self.paths[1].write_text("unknown_option: true\n")
        with patch("jetson_tests.cli.create_campaign") as create:
            rc, output = self.invoke("run")
        self.assertEqual(rc, 2)
        create.assert_not_called()
        self.assertIn("Unknown campaign", output)

    def test_multiple_configs_cannot_share_one_destination_or_resume(self):
        for command, extra in [("run", ["--campaign", str(self.base / "destination")]),
                               ("resume", ["--campaign", str(self.base / "existing")])]:
            with self.subTest(command=command), patch("jetson_tests.cli.create_campaign") as create:
                rc, output = self.invoke(command, *extra)
                self.assertEqual(rc, 2)
                create.assert_not_called()

    def test_doctor_checks_campaigns_sequentially_and_stops_on_error(self):
        for outcomes in ([True, True], [False]):
            with self.subTest(outcomes=outcomes):
                with patch("jetson_tests.cli.create_campaign", side_effect=[self.base / "first", self.base / "second"]), \
                        patch("jetson_tests.cli.preflight", side_effect=[
                            {"ok": ok, "errors": [], "placements": {}} for ok in outcomes
                        ]) as preflight:
                    rc, output = self.invoke("doctor")
                self.assertEqual(rc, 0 if all(outcomes) else 2)
                self.assertEqual(preflight.call_count, len(outcomes))


if __name__ == "__main__":
    unittest.main()
