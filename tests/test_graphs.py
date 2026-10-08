import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'Optional reports dependency')
class GraphTests(unittest.TestCase):
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
