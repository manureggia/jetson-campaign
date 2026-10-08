import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import telegram_control as tg
from jetson_tests.common import save_json


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve()
        save_json(self.out / 'campaign.json', {'id': 'test', 'attempts': []})
        save_json(self.out / 'provenance.json', {'code_hash': 'same'})
        save_json(self.out / 'plan.json', {'experiments': [{'core': 0, 'scenario': 'demo'}]})
        self.control = tg.Control(self.out)

    def test_authorization_requires_private_fresh_sender(self):
        update = {'message': {'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42},
                              'date': 100, 'text': '/restart'}}
        self.assertTrue(tg.authorized(update, 42, 90))
        self.assertFalse(tg.authorized(update, 41, 90))
        self.assertFalse(tg.authorized(update, 42, 101))
        update['message']['from']['id'] = 7
        self.assertFalse(tg.authorized(update, 42, 90))
        update['message']['from']['id'] = 42
        update['message']['chat']['type'] = 'group'
        self.assertFalse(tg.authorized(update, 42, 90))
        self.assertFalse(tg.authorized({'edited_message': update['message']}, 42, 90))
        callback = {'callback_query': {'from': {'id': 42}, 'data': 'perf:id:fallback',
                                      'message': {'chat': {'id': 42, 'type': 'private'}}}}
        self.assertTrue(tg.authorized_callback(callback, 42))
        callback['callback_query']['from']['id'] = 7
        self.assertFalse(tg.authorized_callback(callback, 42))

    def test_latest_campaign_uses_created_timestamp(self):
        older = self.out / 'results/older'
        newer = self.out / 'results/newer'
        save_json(older / 'campaign.json', {'id': 'older', 'created': '2026-01-01T00:00:00+00:00'})
        save_json(newer / 'campaign.json', {'id': 'newer', 'created': '2026-02-01T00:00:00+00:00'})
        self.assertEqual(tg.latest_campaign(self.out / 'results'), newer)

    def test_human_readable_formatters(self):
        snapshot = {'campaign': 'test', 'attempt': 'core0/demo/measurement_001/attempt_001',
                    'alive': True, 'downloaded': False,
                    'state': {'phase': 'RUNNING', 'outcome': 'INCOMPLETE', 'pass': 'l2'},
                    'logs': [{'file': 'pass_l2/demo.stderr.txt', 'tail': '\x1b[31mproblem\x1b[0m'}]}
        status = tg.format_status(snapshot)
        logs = tg.format_logs(snapshot)
        self.assertIn('Worker: attivo', status)
        self.assertIn('Passata: l2', status)
        self.assertNotIn('{', status)
        self.assertIn('problem', logs)
        self.assertNotIn('\x1b', logs)

    def test_progress_and_eta_use_plan_and_observed_durations(self):
        first = {'core': 0, 'scenario': 'demo', 'kind': 'measurement', 'run': 1,
                 'passes': ['core', 'l2'], 'duration_s': 60}
        second = first | {'run': 2}
        save_json(self.out / 'plan.json', {'experiments': [first, second]})
        ledger = {'attempts': [
            {'item': first, 'relative': 'a1', 'generation': 0, 'outcome': 'PASS',
             'download_verified': True, 'retries_left': 2,
             'created': '2026-01-01T12:00:00+00:00', 'finished': '2026-01-01T12:02:00+00:00'},
            {'item': second, 'relative': 'a2', 'generation': 0, 'outcome': 'INCOMPLETE',
             'download_verified': False, 'retries_left': 2, 'created': '2026-01-01T12:02:00+00:00'},
        ]}
        snapshot = {'campaign': 'test', 'attempt': 'a2', 'alive': True,
                    'state': {'phase': 'RUNNING', 'outcome': 'INCOMPLETE', 'pass': 'l2',
                              'timestamp': '2026-01-01T12:02:00+00:00'}}
        progress = tg.campaign_progress(self.out, ledger, snapshot,
                                        datetime(2026, 1, 1, 12, 2, 30, tzinfo=timezone.utc))
        self.assertEqual(progress['completed'], 1)
        self.assertAlmostEqual(progress['percent'], 87.5)
        self.assertAlmostEqual(progress['remaining_s'], 30)
        text = tg.format_status(snapshot | {'progress': progress})
        self.assertIn('Test conclusi: 1/2', text)
        self.assertIn('Tempo residuo stimato: 1 min', text)

    def test_shell_text_is_not_executed(self):
        with patch.object(self.control, 'resume') as resume:
            self.assertEqual(self.control.handle('rm -rf /', 1), tg.HELP)
            self.control.handle('/restart ; touch /tmp/no', 2)
            resume.assert_not_called()

    def test_perf_callback_is_persistent_and_request_bound(self):
        request = {'request_id': 'abc', 'configured': [4, 5], 'online': list(range(6)),
                   'conflicts': [{'core': 0, 'scenario': 'system', 'interferers': [1, 2, 3, 4, 5],
                                  'collision': [4, 5], 'offline': [], 'free': []}],
                   'options': ['collision', 'stop']}
        save_json(self.control.directory / 'perf-placement-request.json', request)
        self.assertIn('Collisione autorizzata', self.control.handle_callback('perf:abc:collision'))
        decision = tg.read_json(self.control.directory / 'perf-placement-decision.json')
        self.assertEqual(decision['action'], 'collision')
        self.assertIn('scaduta', self.control.handle_callback('perf:old:stop'))
        self.assertIn('Conflitto placement perf', tg.format_perf_request(request))

    def test_external_controller_and_changed_code_not_interrupted(self):
        with patch.object(tg, 'provenance', return_value={'code_hash': 'same'}), \
             patch.object(tg.fcntl, 'flock', side_effect=BlockingIOError), \
             patch.object(self.control, 'snapshot') as snapshot, \
             patch.object(tg.subprocess, 'Popen') as spawn:
            self.assertIn('altro terminale', self.control.resume(True))
            snapshot.assert_not_called()
            spawn.assert_not_called()
        with patch.object(tg, 'provenance', return_value={'code_hash': 'changed'}), \
             patch.object(self.control, 'snapshot') as snapshot:
            self.assertIn('Codice cambiato', self.control.resume(True))
            snapshot.assert_not_called()

    def test_restart_preserves_selection_and_does_not_forward_token(self):
        child = Mock()
        child.poll.return_value = None
        self.control.child = child
        with patch.object(tg, 'provenance', return_value={'code_hash': 'same'}), \
             patch.object(self.control, 'snapshot') as snapshot, \
             patch.dict(tg.os.environ, {'TELEGRAM_BOT_TOKEN': 'secret'}), \
             patch.object(tg.subprocess, 'Popen') as spawn:
            self.control.resume(True)
            child.send_signal.assert_called_once_with(tg.signal.SIGINT)
            child.wait.assert_called_once()
            snapshot.assert_called_once_with('stop')
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[-4:], ['--core', '0', '--scenario', 'demo'])
            self.assertIn('--rerun-failed', argv)
            self.assertNotIn('TELEGRAM_BOT_TOKEN', spawn.call_args.kwargs['env'])

    def test_remote_stop_checks_boot_and_identity(self):
        from jetson_tests import worker, processes
        identity = {'pid': 12345, 'start': '10'}
        for saved_boot, matching, expected in [('boot', True, 1), ('old', True, 0), ('boot', False, 0)]:
            with self.subTest(saved_boot=saved_boot, matching=matching), \
                 patch.object(tg.sys, 'argv', ['remote', str(self.out), 'run/attempt', 'stop']), \
                 patch.object(worker, 'checked_root', return_value=self.out), \
                 patch.object(worker, 'status', return_value={'alive': True}), \
                 patch('jetson_tests.common.read_json', return_value={'identity': identity, 'boot_id': saved_boot}), \
                 patch.object(processes, 'boot_id', return_value='boot'), \
                 patch.object(processes, 'matches', return_value=matching), \
                 patch.object(tg.os, 'kill') as kill, patch('builtins.print'):
                exec(tg.REMOTE, {})
                self.assertEqual(kill.call_count, expected)
                if expected:
                    kill.assert_called_with(12345, tg.signal.SIGTERM)

    def test_api_errors_hide_token(self):
        with patch.object(tg.urllib.request, 'urlopen', side_effect=OSError('https://api.telegram.org/botSECRET/getMe')):
            with self.assertRaises(RuntimeError) as caught:
                tg.Telegram('SECRET').call('getMe')
            self.assertNotIn('SECRET', str(caught.exception))

    def test_ollama_grammar_compatibility_keeps_local_length_check(self):
        from jetson_tests.llm import SCHEMA, validate_response
        self.assertNotIn('maxLength', SCHEMA['properties']['reason'])
        value = {'classification': 'normal', 'action': 'continue',
                 'confidence': .9, 'reason': 'x' * 2001}
        with self.assertRaises(ValueError):
            validate_response(value, ['continue'])

    def test_diagnose_disabled_logs_without_action(self):
        config = {'llm': {'enabled': False, 'backend': 'ollama', 'model': 'test',
                         'confidence_threshold': .85, 'required_features': []}}
        save_json(self.out / 'config.json', config)
        with patch.object(self.control, 'snapshot', return_value={'logs': ['test log']}), \
             patch.object(self.control, 'resume') as resume:
            result = self.control.handle('/diagnose', 42)
            self.assertIn('Modello: disabled', result)
            self.assertTrue((self.control.directory / 'llm/42.json').exists())
            resume.assert_not_called()


if __name__ == '__main__':
    unittest.main()
