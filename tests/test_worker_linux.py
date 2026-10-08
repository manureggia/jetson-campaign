"""Real remote lifecycle and reboot reconciliation; never grant perf/RT privileges."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from jetson_tests.common import read_json, save_json
from jetson_tests.config import validate, matrix
from jetson_tests.processes import Processes, boot_id, process_info
from jetson_tests.transport import verify_download
from jetson_tests.worker import launch, reconcile, status


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("JETSON_TEST_ROOT"), "Dedicated Jetson workspace required")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["JETSON_TEST_ROOT"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.relative = "core0/baseline/measurement_001/attempt_001"
        self.out = self.root / "results" / self.relative
        self.out.mkdir(parents=True)

    def test_detached_launch_idempotence_and_precheck_failure(self):
        code = self.root / "code/jetson_tests"
        shutil.copytree(Path(__file__).resolve().parents[1] / "jetson_tests", code, ignore=shutil.ignore_patterns("__pycache__"))
        config = validate({"victim_cores": [0], "profiling": {"enabled": False}})
        save_json(self.root / "config.json", config)
        save_json(self.root / "doctor/doctor.json", {"ok": False, "errors": ["Deliberately missing preflight authorization"], "boot_id": boot_id()})
        request_path = self.root / "request.json"
        save_json(request_path, {"relative": self.relative, "item": matrix(config)[0]})
        first = launch(self.root, request_path)
        self.assertEqual(first, launch(self.root, request_path))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = status(self.root, self.relative)
            if current["manifest_ready"]:
                break
            time.sleep(.1)
        self.assertTrue(current["manifest_ready"])
        self.assertEqual(current["state"]["outcome"], "INCOMPLETE")
        self.assertEqual(read_json(self.out / "failure.json")["category"], "external")
        verify_download(self.out)

    def test_reboot_identity_does_not_signal_current_process(self):
        manager = Processes(self.out, time.monotonic() + 20, .2)
        self.addCleanup(manager.cleanup)
        job = manager.spawn("unrelated_after_reboot", ["sleep", "10"])
        records = read_json(self.out / "processes.json")
        records[0]["boot_id"] = "previous-boot"
        save_json(self.out / "processes.json", records)
        save_json(self.out / "launch.json", {"identity": process_info(os.getpid()), "boot_id": "previous-boot"})
        reconcile(self.root, self.relative)
        self.assertIsNone(job["proc"].poll())
        self.assertEqual(read_json(self.out / "status.json")["outcome"], "INCOMPLETE")

    def test_dead_worker_owned_processes_are_reaped(self):
        manager = Processes(self.out, time.monotonic() + 20, .2)
        self.addCleanup(manager.cleanup)
        job = manager.spawn("owned", ["sleep", "10"])
        save_json(self.out / "launch.json", {"identity": {"pid": 99999999, "start": "0"}, "boot_id": boot_id()})
        reconcile(self.root, self.relative)
        self.assertIsNotNone(job["proc"].poll())
        self.assertFalse(read_json(self.out / "reconciliation.json")["survivors"])


if __name__ == "__main__":
    unittest.main()
