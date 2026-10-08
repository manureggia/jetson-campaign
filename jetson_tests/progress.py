"""Campaign progress shared by the terminal and Telegram companion."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import statistics
import sys
import time

from .common import read_json


def _item_key(item):
    return tuple(item.get(key) for key in ('core', 'scenario', 'kind', 'run'))


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def campaign_progress(directory, ledger, snapshot, now=None, *, plan=None, config=None):
    if plan is None:
        plan = read_json(Path(directory) / 'plan.json', {}).get('experiments', [])
    if not plan:
        return None
    config = config if config is not None else read_json(Path(directory) / 'config.json', {})
    now = now or datetime.now(timezone.utc)
    attempts = ledger.get('attempts', [])
    rows = {}
    for attempt in attempts:
        rows.setdefault(_item_key(attempt.get('item', {})), []).append(attempt)

    def pass_budget(item):
        spec = config.get('scenarios', {}).get(item.get('scenario'), {})
        if item.get('scenario') == 'demo':
            spec = spec.get('definition', {})
        return item.get('duration_s', 0) + spec.get('warmup_s', 0) + config.get('cooldown_s', 0)

    budgets = {_item_key(item): len(item.get('passes', [])) * pass_budget(item) for item in plan}
    complete, passed, failed = set(), 0, 0
    ratios = {}
    for item in plan:
        key = _item_key(item)
        history = rows.get(key, [])
        if not history:
            continue
        generation = max(row.get('generation', 0) for row in history)
        current = [row for row in history if row.get('generation', 0) == generation]
        winner = next((row for row in current if row.get('outcome') == 'PASS'), None)
        last = winner or current[-1]
        exhausted = last.get('outcome') == 'FAIL' and last.get('retries_left', 0) <= 0
        if winner or exhausted:
            complete.add(key)
            passed += bool(winner)
            failed += bool(exhausted)
        if winner:
            started, finished = _time(winner.get('created')), _time(winner.get('finished'))
            weight = budgets[key]
            elapsed = winner.get('worker_elapsed_s')
            if elapsed is None and started and finished:
                elapsed = (finished - started).total_seconds()
            if elapsed is not None and elapsed > 0 and weight > 0:
                ratios.setdefault((item.get('scenario'), item.get('kind')), []).append(
                    elapsed / weight)

    weights = {_item_key(item): len(item.get('passes', [])) * item.get('duration_s', 0) for item in plan}
    fractions = {key: 1.0 for key in complete}
    active = next((row for row in attempts if row.get('relative') == snapshot.get('attempt')), None)
    current, active_remaining = None, None
    if active and not active.get('download_verified') and _item_key(active['item']) in weights:
        item = active['item']
        passes = item.get('passes', [])
        pass_name = (snapshot.get('state') or {}).get('pass')
        pass_index = passes.index(pass_name) if pass_name in passes else 0
        phase = (snapshot.get('state') or {}).get('phase')
        started = _time((snapshot.get('state') or {}).get('timestamp'))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        within = 0.0
        if phase == 'RUNNING':
            if started and item.get('duration_s', 0) > 0:
                within = min(1.0, elapsed / item['duration_s'])
        after_window = {'STOP_WORKLOADS', 'STOP_MONITORS', 'SNAPSHOT_AFTER', 'PROCESS_RESULTS', 'VALIDATE', 'COMPLETE'}
        if phase in after_window:
            within = 1.0
        if phase == 'CLEANUP' and not (snapshot.get('state') or {}).get('error'):
            within = 1.0
        if phase in {'FAILED', 'CLEANUP', 'COMPLETE'} and (snapshot.get('state') or {}).get('error'):
            within = 0.0
        fractions[_item_key(item)] = (pass_index + within) / max(1, len(passes))
        budget = pass_budget(item)
        active_remaining = budgets[_item_key(item)]
        if pass_name in passes and not (snapshot.get('state') or {}).get('error'):
            active_remaining = (len(passes) - pass_index - 1) * budget
            if phase == 'RUNNING':
                active_remaining += max(0, item['duration_s'] - elapsed) + config.get('cooldown_s', 0)
            elif phase in after_window or (phase == 'CLEANUP' and within == 1):
                active_remaining += config.get('cooldown_s', 0)
            elif phase == 'WARMUP':
                warmup = budget - item['duration_s'] - config.get('cooldown_s', 0)
                active_remaining += max(0, warmup - elapsed) + item['duration_s'] + config.get('cooldown_s', 0)
            else:
                active_remaining += budget
        current = {'position': next((i for i, row in enumerate(plan, 1) if _item_key(row) == _item_key(item)), 0),
                   'core': item.get('core'), 'scenario': item.get('scenario'), 'kind': item.get('kind'),
                   'run': item.get('run'), 'pass': pass_name, 'pass_position': pass_index + 1,
                   'pass_total': len(passes), 'pass_elapsed_s': elapsed if phase == 'RUNNING' else None,
                   'duration_s': item.get('duration_s', 0)}

    total_weight = sum(weights.values())
    done_weight = sum(weights[key] * fraction for key, fraction in fractions.items())
    percent = 100.0 if total_weight == 0 else 100 * done_weight / total_weight
    if len(complete) < len(plan):
        percent = min(99.9, percent)
    all_ratios = [value for values in ratios.values() for value in values]

    def factor(item):
        values = ratios.get((item.get('scenario'), item.get('kind'))) or all_ratios
        return statistics.median(values) if values else 1.0

    remaining_s = sum((active_remaining if current and _item_key(item) == _item_key(active['item'])
                       else budgets[_item_key(item)]) * factor(item)
                      for item in plan if _item_key(item) not in complete)
    next_item = next((item for item in plan if _item_key(item) not in complete), None)
    return {'percent': percent, 'completed': len(complete), 'passed': passed, 'failed': failed,
            'total': len(plan), 'current': current, 'next': next_item, 'remaining_s': remaining_s,
            'finish': (now + timedelta(seconds=remaining_s)).isoformat(),
            'observed_eta': bool(all_ratios)}


def clock_text(seconds):
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


class TerminalProgress:
    """Redraw a small terminal panel; redirected output gets periodic plain snapshots."""
    def __init__(self, directory, ledger, plan, config, *, reset_items=(), settled_items=(), stream=None):
        self.directory, self.ledger, self.plan, self.config = directory, ledger, plan, config
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.started = time.monotonic()
        self.last_render = self.started - 30
        self.lines = 0
        self.signature = None
        self.snapshot = {}
        reset = {_item_key(item) for item in reset_items}
        self.ignored = {row['relative'] for row in ledger['attempts']
                        if row.get('download_verified') and _item_key(row['item']) in reset}
        self.settled = {_item_key(item) for item in settled_items}

    def clear(self):
        if self.lines:
            self.stream.write(f'\x1b[{self.lines}F\x1b[J')
            self.stream.flush()
            self.lines = 0

    def message(self, text):
        self.clear()
        print(text, file=self.stream, flush=True)

    def update(self, snapshot=None, *, phase=None, force=False):
        if snapshot is not None:
            self.snapshot = snapshot
        snapshot = self.snapshot
        attempts = [row | {'retries_left': 0} if _item_key(row['item']) in self.settled else row
                    for row in self.ledger['attempts'] if row['relative'] not in self.ignored]
        progress = campaign_progress(self.directory, {'attempts': attempts}, snapshot,
                                     plan=self.plan, config=self.config)
        if not progress:
            return
        state = snapshot.get('state') or {}
        phase = phase or state.get('phase', 'IN ATTESA')
        signature = (snapshot.get('attempt'), phase, state.get('pass'),
                     progress['completed'], progress['passed'], progress['failed'])
        elapsed = time.monotonic() - self.started
        if not (force or self.interactive or signature != self.signature or time.monotonic() - self.last_render >= 30):
            return
        self.signature, self.last_render = signature, time.monotonic()
        filled = int(progress['percent'] * 20 / 100)
        lines = [f"[{'#' * filled}{'-' * (20 - filled)}] {progress['percent']:.1f}% acquisizione | "
                 f"Test conclusi {progress['completed']}/{progress['total']} "
                 f"(PASS {progress['passed']}, FAIL {progress['failed']})"]
        current = progress['current']
        if current:
            index, attempt = next((i, row) for i, row in enumerate(self.ledger['attempts'])
                                  if row['relative'] == snapshot['attempt'])
            number = sum(_item_key(row['item']) == _item_key(attempt['item'])
                         for row in self.ledger['attempts'][:index + 1])
            lines.append(f"Test {current['position']}/{progress['total']} | CPU{current['core']} "
                         f"{current['scenario']} | {current['kind']} | Ripetizione {current['run']} | Tentativo {number}")
            if current['pass']:
                window = (f" | {clock_text(current['pass_elapsed_s'])}/{clock_text(current['duration_s'])}"
                          if current['pass_elapsed_s'] is not None else '')
                lines.append(f"Passata {current['pass_position']}/{current['pass_total']}: {current['pass']}{window}")
        elif progress['next']:
            item = progress['next']
            lines.append(f"Prossimo test: CPU{item['core']} {item['scenario']} | {item['kind']} | Ripetizione {item['run']}")
        lines.append('Fase: ' + phase)
        source = 'tempi osservati' if progress['observed_eta'] else 'durate nominali'
        if progress['completed'] == progress['total']:
            remaining = 'Test conclusi'
        elif not progress['remaining_s']:
            remaining = 'Finalizzazione in corso'
        else:
            finish = _time(progress['finish']).astimezone().strftime('%d/%m %H:%M')
            remaining = f"Mancano ~{clock_text(progress['remaining_s'])} | Fine ~{finish} ({source})"
        lines.append(f'Trascorso (sessione): {clock_text(elapsed)}')
        lines.append(remaining)
        self.clear()
        if self.interactive:
            width = max(1, shutil.get_terminal_size().columns - 1)
            lines = [line if len(line) <= width else line[:width - 1] + '…' for line in lines]
        self.stream.write('\n'.join(lines) + '\n')
        self.stream.flush()
        self.lines = len(lines) if self.interactive else 0

    def close(self):
        # Keep the final snapshot visible, but stop treating it as a live panel.
        self.lines = 0
