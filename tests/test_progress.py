"""Progress and terminal rendering without launching Jetson workloads."""
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jetson_tests.progress import TerminalProgress, campaign_progress


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.item = {'core': 3, 'scenario': 'demo', 'kind': 'measurement', 'run': 1,
                     'passes': ['core', 'victim_instructions'], 'duration_s': 60}
        self.attempt = {'item': self.item, 'relative': 'attempt_001', 'generation': 0,
                        'outcome': 'INCOMPLETE', 'download_verified': False, 'retries_left': 1}
        self.ledger = {'attempts': [self.attempt]}
        self.now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
        self.snapshot = {'attempt': 'attempt_001', 'alive': True,
                         'state': {'phase': 'RUNNING', 'pass': 'core',
                                   'timestamp': '2026-01-01T12:00:00+00:00'}}
        self.config = {'cooldown_s': 10, 'scenarios': {'demo': {'definition': {'warmup_s': 20}}}}

    def progress(self, **kwargs):
        return campaign_progress(self.out, self.ledger, self.snapshot, self.now,
                                 plan=[self.item], config=self.config, **kwargs)

    def test_nominal_eta_includes_remaining_window_warmup_and_cooldown(self):
        result = self.progress()
        self.assertAlmostEqual(result['percent'], 25)
        self.assertEqual(result['remaining_s'], 130)  # 30 + 10 now, then 20 + 60 + 10.
        self.assertEqual(result['current']['pass_elapsed_s'], 30)
        self.snapshot['state']['phase'] = 'WARMUP'
        self.snapshot['state']['timestamp'] = '2026-01-01T12:00:20+00:00'
        self.assertEqual(self.progress()['remaining_s'], 170)
        self.assertEqual(self.progress()['percent'], 0)

    def test_observed_eta_and_selected_plan(self):
        previous_item = self.item | {'run': 2}
        previous = self.attempt | {'item': previous_item, 'relative': 'old', 'outcome': 'PASS',
                                  'download_verified': True,
                                  'created': '2026-01-01T11:00:00+00:00',
                                  'finished': '2026-01-01T11:06:00+00:00'}
        self.ledger['attempts'].insert(0, previous)
        result = campaign_progress(self.out, self.ledger, self.snapshot, self.now,
                                   plan=[previous_item, self.item], config=self.config)
        self.assertAlmostEqual(result['percent'], 62.5)
        self.assertEqual(result['remaining_s'], 260)
        self.assertTrue(result['observed_eta'])
        previous['worker_elapsed_s'] = 180
        previous['finished'] = '2026-01-02T11:06:00+00:00'  # Controller resumed a day later.
        result = campaign_progress(self.out, self.ledger, self.snapshot, self.now,
                                   plan=[previous_item, self.item], config=self.config)
        self.assertEqual(result['remaining_s'], 130)
        # An outstanding attempt outside a filtered plan must not affect its progress.
        self.snapshot['attempt'] = 'old'
        result = self.progress()
        self.assertEqual(result['percent'], 0)
        self.assertIsNone(result['current'])

    def test_finalization_is_not_complete_and_failure_does_not_finish_a_window(self):
        self.snapshot['state'].update(phase='CLEANUP', **{'pass': 'victim_instructions'})
        self.assertEqual(self.progress()['percent'], 99.9)
        self.snapshot['alive'] = False
        self.snapshot['state']['phase'] = 'COMPLETE'
        self.assertEqual(self.progress()['percent'], 99.9)
        self.snapshot['state'].update(phase='CLEANUP', error='interrupted', **{'pass': 'core'})
        self.assertEqual(self.progress()['percent'], 0)
        self.assertEqual(self.progress()['remaining_s'], 180)
        self.attempt.update(outcome='PASS', download_verified=True)
        self.assertEqual(self.progress()['percent'], 100)
        self.assertEqual(self.progress()['remaining_s'], 0)

    def test_plain_logs_show_details_and_are_throttled_until_a_phase_change(self):
        stream = io.StringIO()
        with patch('jetson_tests.progress.time.monotonic', return_value=100) as clock:
            panel = TerminalProgress(self.out, self.ledger, [self.item], self.config, stream=stream)
            panel.update(self.snapshot)
            text = stream.getvalue()
            for detail in ('CPU3 demo', 'Ripetizione 1', 'Tentativo 1', 'Passata 1/2: core',
                           'Fase: RUNNING', 'Trascorso (sessione)', 'Mancano ~', 'Fine ~'):
                self.assertIn(detail, text)
            self.assertNotIn('\x1b', text)
            clock.return_value = 110
            panel.update(self.snapshot)
            self.assertEqual(stream.getvalue(), text)
            clock.return_value = 131
            panel.update(self.snapshot)
            self.assertGreater(len(stream.getvalue()), len(text))
            text = stream.getvalue()
            panel.update(self.snapshot | {'state': {'phase': 'WARMUP', 'pass': 'victim_instructions'}})
            self.assertGreater(len(stream.getvalue()), len(text))

    def test_terminal_redraw_respects_width_and_messages_survive(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        stream = Terminal()
        with patch.dict('os.environ', {'TERM': 'xterm', 'COLUMNS': '80'}):
            panel = TerminalProgress(self.out, self.ledger, [self.item], self.config, stream=stream)
            panel.update(self.snapshot)
            self.assertTrue(all(len(line) <= 79 for line in stream.getvalue().splitlines()))
            self.assertIn('Mancano ~', stream.getvalue())
            panel.update(self.snapshot)
            self.assertIn('\x1b[6F\x1b[J', stream.getvalue())
            panel.message('SSH unavailable')
            before = stream.getvalue()
            panel.update(self.snapshot)
            self.assertNotIn('\x1b', stream.getvalue()[len(before):])
            panel.close()
            self.assertEqual(panel.lines, 0)

    def test_reset_generations_and_settled_failures(self):
        self.attempt.update(outcome='PASS', download_verified=True)
        stream = io.StringIO()
        panel = TerminalProgress(self.out, self.ledger, [self.item], self.config,
                                 reset_items=[self.item], stream=stream)
        panel.update()
        self.assertIn('Test conclusi 0/1', stream.getvalue())
        new = self.attempt | {'relative': 'attempt_002', 'generation': 1,
                             'outcome': 'INCOMPLETE', 'download_verified': False}
        self.ledger['attempts'].append(new)
        panel.update(self.snapshot | {'attempt': 'attempt_002'})
        self.assertIn('Tentativo 2', stream.getvalue())
        new.update(outcome='FAIL', download_verified=True)
        panel = TerminalProgress(self.out, self.ledger, [self.item], self.config,
                                 settled_items=[self.item], stream=stream)
        panel.update()
        self.assertIn('Test conclusi 1/1 (PASS 0, FAIL 1)', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
