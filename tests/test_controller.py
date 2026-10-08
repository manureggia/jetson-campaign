"""End-to-end orchestration through a stateful fake SSH transport, not fake hardware results."""
import argparse
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from jetson_tests.cli import create_campaign, run_campaign, wait_attempt
from jetson_tests.common import read_json, save_json
from jetson_tests.config import validate, matrix
from jetson_tests.worker import finish_manifest


class FakeTransport:
    def __init__(self, remote):
        self.remote = remote
        self.requests = {}
        self.launches = []
        self.hooks = []
        self.disconnected = False

    def upload(self, *args):
        pass

    def put_json(self, path, value):
        self.requests[path] = value

    def worker(self, root, operation, **kwargs):
        if operation == "doctor":
            self.remote.joinpath("doctor").mkdir(exist_ok=True)
            value = {"ok": True, "errors": [], "placements": {}}
            save_json(self.remote / "doctor/doctor.json", value)
            return value
        if operation == "launch":
            request = self.requests[kwargs["request"]]
            self.launches.append(request)
            out = self.remote / "results" / request["relative"]
            out.mkdir(parents=True)
            save_json(out / "status.json", {"phase": "COMPLETE", "outcome": "PASS"})
            save_json(out / "metadata.json", {"started": "2026-01-01T12:00:00+00:00",
                                              "finished": "2026-01-01T12:02:00+00:00"})
            # No synthetic PMU values: this fixture tests delivery/state only.
            save_json(out / "metrics.json", {"passes": []})
            finish_manifest(out)
            return {}
        if operation == "hook":
            self.hooks.append(kwargs["phase"])
            save_json(self.remote / "hooks/state.json", {"phase": kwargs["phase"]})
            return {"phase": kwargs["phase"]}
        if operation == "status":
            if not self.disconnected:
                self.disconnected = True
                raise RuntimeError("simulated SSH disconnect")
            return {"alive": False, "manifest_ready": True}
        raise AssertionError(operation)

    def fetch(self, remote, local):
        suffix = remote.split("/results/", 1)
        if len(suffix) == 2:
            source = self.remote / "results" / suffix[1]
        elif remote.endswith("/hooks"):
            source = self.remote / "hooks"
        else:
            source = self.remote / "doctor"
        shutil.copytree(source, local, dirs_exist_ok=True)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = validate({"victim_cores": [0], "profiling": {"enabled": False}})
        self.out = create_campaign(self.config, {"campaign.yaml": "name: test"}, self.base / "campaign")
        self.transport = FakeTransport(self.base / "remote")
        self.transport.remote.mkdir()
        self.args = argparse.Namespace(core=None, scenario=None, force=False, rerun_failed=False)

    def test_campaign_names_use_numeric_suffix(self):
        config = copy.deepcopy(self.config)
        config.update(name="semplice", results_dir=str(self.base / "named"))
        reserve = lambda name: name != "semplice"
        first = create_campaign(config, {"campaign.yaml": "name: semplice"}, reserve_remote=reserve)
        self.assertEqual(first.name, "semplice_1")
        self.assertFalse((self.base / "named/semplice").exists())
        second = create_campaign(config, {"campaign.yaml": "name: semplice"})
        third = create_campaign(config, {"campaign.yaml": "name: semplice"})
        self.assertEqual((second.name, third.name), ("semplice", "semplice_2"))
        self.assertEqual(read_json(first / "campaign.json")["remote"],
                         "/home/nvidia/codex-work/semplice_1")

    def test_disconnect_download_and_resume_skips_completed(self):
        with patch("jetson_tests.cli.time.sleep"):
            run_campaign(self.out, self.config, self.args, self.transport)
            run_campaign(self.out, self.config, self.args, self.transport)
        self.assertEqual(len(self.transport.launches), 1)
        self.assertTrue(read_json(self.out / "campaign.json")["attempts"][0]["download_verified"])
        self.assertEqual(read_json(self.out / "campaign.json")["attempts"][0]["worker_elapsed_s"], 120)
        self.assertTrue((self.out / "report/runs.csv").exists())

    def test_cli_displays_polled_passes_and_final_result(self):
        original = self.transport.worker
        states = iter([
            {"alive": True, "state": {"phase": "WARMUP", "pass": "core"}},
            {"alive": True, "state": {"phase": "RUNNING", "pass": "core"}},
            {"alive": True, "state": {"phase": "RUNNING", "pass": "victim_l2"}},
            {"alive": False, "manifest_ready": True, "state": {"phase": "COMPLETE", "pass": "victim_l3"}},
        ])
        def worker(root, operation, **kwargs):
            return next(states) if operation == "status" else original(root, operation, **kwargs)
        output = io.StringIO()
        with patch.object(self.transport, "worker", side_effect=worker), \
                patch("jetson_tests.cli.time.sleep"), redirect_stdout(output):
            run_campaign(self.out, self.config, self.args, self.transport)
        text = output.getvalue()
        for expected in ("Test 1/1", "Passata 1/12: core", "Passata 11/12: victim_l2",
                         "Fase: WARMUP", "Fase: RUNNING", "Mancano ~", "100.0%", "PASS 1, FAIL 0"):
            self.assertIn(expected, text)
        self.assertNotIn("\x1b", text)

    def test_force_preserves_previous_attempt(self):
        with patch("jetson_tests.cli.time.sleep"):
            run_campaign(self.out, self.config, self.args, self.transport)
            self.args.force = True
            run_campaign(self.out, self.config, self.args, self.transport)
        attempts = read_json(self.out / "campaign.json")["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]["relative"], attempts[1]["relative"])
        self.assertGreater(attempts[1]["generation"], attempts[0]["generation"])

    def test_resume_rejects_changed_config(self):
        changed = copy.deepcopy(self.config)
        changed["duration_s"] += 1
        with self.assertRaisesRegex(ValueError, "Configuration/profile changed"):
            run_campaign(self.out, changed, self.args, self.transport)
        self.assertFalse(self.transport.launches)

    def test_global_hooks_wrap_complete_matrix(self):
        config = validate({"victim_cores": [0], "profiling": {"enabled": False},
                           "command-pre-test": ["true"], "command-post-test": ["true"]})
        out = create_campaign(config, {"campaign.yaml": "name: hooks"}, self.base / "hooks-campaign")
        transport = FakeTransport(self.base / "hooks-remote")
        transport.remote.mkdir()
        with patch("jetson_tests.cli.time.sleep"):
            run_campaign(out, config, self.args, transport)
        self.assertEqual(transport.hooks, ["pre", "post"])

    def test_filtered_success_keeps_global_hook_active(self):
        config = validate({"victim_cores": [0, 3], "profiling": {"enabled": False},
                           "command-pre-test": ["true"], "command-post-test": ["true"]})
        out = create_campaign(config, {"campaign.yaml": "name: filtered"}, self.base / "filtered-campaign")
        transport = FakeTransport(self.base / "filtered-remote")
        transport.remote.mkdir()
        args = argparse.Namespace(core=0, scenario=None, force=False, rerun_failed=False)
        with patch("jetson_tests.cli.time.sleep"):
            run_campaign(out, config, args, transport)
        self.assertEqual(transport.hooks, ["pre"])


if __name__ == "__main__":
    unittest.main()
