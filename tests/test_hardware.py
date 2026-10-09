import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jetson_tests.config import validate
from jetson_tests.hardware import doctor


class DoctorWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = validate({"victim_cores": [3], "perf_passes": {"core": ["task_clock"]},
                                "profiling": {"enabled": False}})
        patches = {
            "machine_info": {"cpu": "0xd42", "rtprio_limit": [99, 99]},
            "topology": {"online": [0, 1, 2, 3, 4, 5]},
            "inventory": {"task_clock": {"available": True, "event": "task-clock"}},
            "shutil.which": "/usr/bin/tool",
            "shutil.disk_usage": SimpleNamespace(free=10**12),
        }
        for name, value in patches.items():
            patcher = patch("jetson_tests.hardware." + name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_active_workload_blocks_all_probes_and_preserves_diagnostics(self):
        self.config["isolcpu"] = [3]
        foreign = [{"pid": 1167873, "executable": "meminterf", "state": "R"}]
        with patch("jetson_tests.hardware.foreign_workloads", return_value=foreign), \
                patch("jetson_tests.hardware.Processes") as processes:
            info = doctor(self.config, self.directory)
        processes.assert_not_called()
        self.assertFalse(info["ok"])
        self.assertIn("Foreign DEMO/INTERFGEN workload active; operator must stop it", info["errors"])
        saved = json.loads((self.directory / "doctor.json").read_text())
        self.assertEqual(saved["foreign_workloads"], foreign)
        self.assertFalse(saved["ok"])

    def test_idle_board_still_runs_requested_probes(self):
        with patch("jetson_tests.hardware.foreign_workloads", return_value=[]), \
                patch("jetson_tests.hardware.Processes") as processes:
            processes.return_value.wait.return_value = 0
            info = doctor(self.config, self.directory)
        processes.assert_called_once()
        names = [call.args[0] for call in processes.return_value.spawn.call_args_list]
        self.assertIn("perf_permission_3", names)
        self.assertIn("event_task_clock", names)
        processes.return_value.cleanup.assert_called_once()
        self.assertTrue(info["ok"], info["errors"])


if __name__ == "__main__":
    unittest.main()
