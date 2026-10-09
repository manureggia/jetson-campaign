import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'Optional reports dependency')
class GraphTests(unittest.TestCase):
    def split_entry(self, metrics, pass_name='victim_core'):
        return {'attempt': {'relative': 'test', 'outcome': 'PASS',
                            'item': {'core': 0, 'scenario': 'demo', 'duration_s': 180}},
                'directory': Path('test'),
                'passes': {pass_name: {'perf': {'victim_task': {
                    key: {'status': 'ok', 'value': value} for key, value in metrics.items()}}}}}

    def test_split_totals_and_ipc_use_same_run_and_pass(self):
        from analysis.plot_campaign import summarize, perf, ratio
        entries = [self.split_entry({'instructions:u': 10, 'instructions:k': 30, 'cycles': 10}),
                   self.split_entry({'instructions:u': 20, 'instructions:k': 80, 'cycles': 30})]
        aggregate, points, sources = summarize(entries, perf('victim_task', 'task_core', 'instructions'), 0, 'demo')
        self.assertEqual((aggregate, points), (70, [40, 100]))
        self.assertEqual(sources[0]['pass'], 'victim_core')
        self.assertEqual(sources[0]['components'], {'instructions:u': 10, 'instructions:k': 30})
        self.assertEqual(summarize(entries, perf('victim_task', 'task_core', 'instructions:u'), 0, 'demo')[0], 15)
        self.assertEqual(summarize(entries, ratio('victim_task', 'task_core', 'instructions', 'cycles', 1), 0, 'demo')[0], 3.5)

    def test_missing_or_invalid_component_is_not_zero(self):
        from analysis.plot_campaign import value, perf
        expression = perf('victim_task', 'task_core', 'instructions')
        entry = self.split_entry({'instructions:u': 0, 'instructions:k': 30})
        self.assertEqual(value(entry, expression), 30)
        del entry['passes']['victim_core']['perf']['victim_task']['instructions:u']
        self.assertIsNone(value(entry, expression))
        self.assertEqual(value(entry, perf('victim_task', 'task_core', 'instructions:k')), 30)
        entry['passes']['victim_core']['perf']['victim_task']['instructions:u'] = {'status': 'unsupported', 'value': 999}
        self.assertIsNone(value(entry, expression))

    def test_non_instruction_split_metric_in_combined_pass(self):
        from analysis.plot_campaign import value, perf, ratio
        entry = self.split_entry({'memory_accesses:u': 8, 'memory_accesses:k': 12,
                                  'memory_accesses': 999, 'cycles:u': 0, 'cycles:k': 50}, 'victim_combined')
        self.assertEqual(value(entry, perf('victim_task', 'task_memory', 'memory_accesses')), 20)
        self.assertEqual(value(entry, ratio('victim_task', 'task_memory', 'memory_accesses', 'cycles')), (20, 50))

    def test_does_not_combine_components_or_ratios_across_passes(self):
        from analysis.plot_campaign import value, perf, ratio
        entry = self.split_entry({'instructions:k': 30})
        entry['passes'].update(self.split_entry({'instructions:u': 10, 'cycles': 10}, 'victim_other')['passes'])
        self.assertIsNone(value(entry, perf('victim_task', 'task_core', 'instructions')))
        self.assertIsNone(value(entry, perf('victim_task', 'task_core', 'instructions:u')))
        self.assertIsNone(value(entry, ratio('victim_task', 'task_core', 'instructions', 'cycles', 1)))

    def test_custom_pass_and_event_get_total_then_split_in_markdown(self):
        from analysis.plot_campaign import build
        entry = self.split_entry({'instructions:u': 10, 'instructions:k': 30,
                                  'branch_predictions:u': 4, 'branch_predictions:k': 6}, 'victim_custom')
        captured = []
        def capture(entries, target, number, slug, title, descriptors, *args):
            captured.append((slug, descriptors))
            return {'name': f'{number:02d}_{slug}', 'title': title, 'partial': False}
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            (campaign / 'campaign.json').write_text(json.dumps({'config_hash': 'test'}))
            (campaign / 'test').mkdir()
            entry['directory'] = campaign / 'test'
            (entry['directory'] / 'manifest.json').write_text('{}')
            with patch('analysis.plot_campaign.load', return_value=[entry]), patch('analysis.plot_campaign.figure', side_effect=capture):
                target = build(campaign)
            slugs = [slug for slug, _ in captured]
            i = slugs.index('istruzioni')
            self.assertEqual(slugs[i + 1], 'istruzioni_user_kernel_1')
            split_events = [d[2][3] for d in captured[i + 1][1]]
            self.assertEqual(split_events, ['instructions:u', 'instructions:k'])
            custom = next(i for i, (slug, _) in enumerate(captured) if 'branch_predictions' in slug)
            self.assertIn('user_kernel', captured[custom + 1][0])
            markdown = (target / 'report.md').read_text()
            self.assertIn('180 s', markdown)
            self.assertIn('CPU 0', markdown)
            self.assertNotIn('CPU 3', markdown)
            self.assertLess(markdown.index('## Istruzioni ritirate e IPC\n'), markdown.index('## Istruzioni ritirate e IPC - user / kernel'))
            self.assertFalse(list(target.glob('*.png')))
            self.assertFalse(list(target.glob('*.pdf')))

    def test_different_window_lengths_are_rejected(self):
        from analysis.plot_campaign import build
        entries = [self.split_entry({}), self.split_entry({})]
        entries[1]['attempt']['item']['duration_s'] = 300
        with patch('analysis.plot_campaign.load', return_value=entries):
            with self.assertRaisesRegex(ValueError, 'finestre diverse'):
                build(Path('unused'))

    def test_victim_prefix_and_historical_task_prefix(self):
        from analysis.plot_campaign import summarize, perf
        for name in ('task_core', 'victim_core'):
            entry = {'attempt': {'relative': 'test', 'outcome': 'PASS',
                                 'item': {'core': 0, 'scenario': 'demo'}},
                     'passes': {name: {'perf': {'victim_task': {
                         'instructions': {'status': 'ok', 'value': 123}}}}}}
            value, _, sources = summarize([entry], perf('victim_task', 'task_core', 'instructions'), 0, 'demo')
            self.assertEqual(value, 123)
            self.assertEqual(sources[0]['pass'], name)

    def test_aggregation_and_partial_separation(self):
        from analysis.plot_campaign import summarize, latency, ratio
        def entry(status, mean, numerator, denominator):
            return {'attempt': {'relative': str(mean), 'outcome': status,
                               'item': {'core': 0, 'scenario': 'demo'}},
                    'passes': {'core': {'latency': {'reported_mean_us': mean, 'min_us': mean},
                                       'perf': {'victim_cpu': {
                                           'l1d_refills': {'status': 'ok', 'value': numerator},
                                           'l1d_accesses': {'status': 'ok', 'value': denominator}}}}}}
        entries = [entry('PASS', 2, 1, 10), entry('PASS', 4, 9, 30), entry('INCOMPLETE', 90, 10, 10)]
        self.assertEqual(summarize(entries, latency('reported_mean_us'), 0, 'demo')[0], 3)
        self.assertEqual(summarize(entries, latency('min_us', 'min'), 0, 'demo')[0], 2)
        self.assertEqual(summarize(entries, latency('reported_mean_us'), 0, 'demo', True)[0], 32)
        result = summarize(entries, ratio('victim_cpu', 'core', 'l1d_refills', 'l1d_accesses'), 0, 'demo')
        self.assertEqual(result[0], 25)  # ratio of sums, not mean of ratios (20).
        self.assertEqual(result[1], [10, 30])
        self.assertEqual(summarize(entries, latency('reported_mean_us'), 3, 'demo'), (None, [], []))
