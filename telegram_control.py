"""Small local Telegram companion; no changes to the measurement protocol."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

from jetson_tests.cli import REPOSITORY, provenance
from jetson_tests.common import append_json, read_json, save_json
from jetson_tests.llm import LLMRequest, decide
from jetson_tests.progress import _time, campaign_progress
from jetson_tests.transport import Transport

HELP = ('/status — stato e heartbeat\n/logs — ultimi log\n'
        '/diagnose — interpretazione Ollama, senza azioni\n'
        '/resume — prendi in carico la campagna e riprendi i tentativi falliti\n'
        '/restart — interrompi il tentativo attivo e riparti, conservando i risultati\n'
        'Un controller attivo in un altro terminale deve prima essere fermato con Ctrl-C.')

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def latest_campaign(results_dir):
    """Return the newest valid campaign by its immutable creation timestamp."""
    candidates = []
    for ledger_path in Path(results_dir).resolve().glob('*/campaign.json'):
        ledger = read_json(ledger_path, {})
        if isinstance(ledger, dict) and ledger.get('id'):
            candidates.append((str(ledger.get('created', '')), ledger_path.stat().st_mtime,
                               ledger_path.parent.name, ledger_path.parent))
    if not candidates:
        raise ValueError('Nessuna campagna trovata in ' + str(Path(results_dir).resolve()))
    return max(candidates)[-1]


def clean(value):
    return ANSI.sub('', str(value)).strip()


def duration_text(seconds):
    minutes = max(0, int((seconds + 59) // 60))
    if minutes < 1:
        return 'meno di un minuto'
    hours, minutes = divmod(minutes, 60)
    return (f'{hours} h {minutes} min' if minutes else f'{hours} h') if hours else f'{minutes} min'


def format_status(snapshot):
    state = snapshot.get('state') or {}
    progress = snapshot.get('progress')
    lines = ['📊 Stato campagna', '', 'Campagna: ' + str(snapshot.get('campaign', '—'))]
    if snapshot.get('attempt'):
        lines.append('Tentativo: ' + str(snapshot['attempt']))
    lines += [
        'Worker: ' + ('attivo' if snapshot.get('alive') else 'fermo'),
        'Fase: ' + str(state.get('phase', 'sconosciuta')),
    ]
    if state.get('outcome'):
        lines.append('Esito: ' + str(state['outcome']))
    if state.get('pass') and not (progress and progress.get('current')):
        lines.append('Passata: ' + str(state['pass']))
    if 'downloaded' in snapshot:
        lines.append('Artefatti: ' + ('scaricati e verificati' if snapshot['downloaded'] else 'non ancora scaricati'))
    if state.get('timestamp'):
        lines.append('Ultimo aggiornamento: ' + str(state['timestamp']))
    if progress:
        filled = min(12, int(progress['percent'] * 12 / 100))
        lines += ['', 'Avanzamento', '[' + '█' * filled + '░' * (12 - filled) + f"] {progress['percent']:.0f}%",
                  f"Test conclusi: {progress['completed']}/{progress['total']} "
                  f"({progress['passed']} PASS, {progress['failed']} FAIL)"]
        current = progress.get('current')
        if current:
            lines.append(f"Test corrente: {current['position']}/{progress['total']} · CPU{current['core']} "
                         f"{current['scenario']} · {current['kind']} {current['run']}")
            if current.get('pass'):
                lines.append(f"Passata: {current['pass_position']}/{current['pass_total']} · {current['pass']}")
        elif progress['completed'] < progress['total'] and progress.get('next'):
            item = progress['next']
            lines.append(f"Prossimo test: CPU{item['core']} {item['scenario']} · {item['kind']} {item['run']}")
        if progress['completed'] < progress['total']:
            finish = _time(progress['finish']).astimezone()
            source = 'tempi osservati' if progress['observed_eta'] else 'durate nominali'
            lines += [f"Tempo residuo stimato: {duration_text(progress['remaining_s'])}",
                      'Fine stimata: ' + finish.strftime('%d/%m alle %H:%M'),
                      f"Stima da {source}, esclusi eventuali retry futuri."]
    heartbeat = snapshot.get('heartbeat') or {}
    if heartbeat.get('timestamp'):
        lines.append('Heartbeat: ' + str(heartbeat['timestamp']))
    failure = snapshot.get('failure') or {}
    if failure:
        details = failure.get('message') or failure.get('error') or failure.get('category')
        lines += ['', '⚠️ Errore: ' + clean(details or failure)]
    return '\n'.join(lines)


def format_logs(snapshot):
    entries = snapshot.get('logs') or []
    if not entries:
        return '📄 Ultimi log\n\nNessun log disponibile.'
    sections = ['📄 Ultimi log', 'Campagna: ' + str(snapshot.get('campaign', '—'))]
    for entry in entries[-3:]:
        if isinstance(entry, dict):
            name = entry.get('file') or entry.get('process') or 'log'
            body = clean(entry.get('tail', entry.get('text', '')))
        else:
            name, body = 'log', clean(entry)
        if body:
            sections += ['', '▸ ' + str(name), body[-450:]]
    return '\n'.join(sections)[:1850] if len(sections) > 2 else '📄 Ultimi log\n\nNessun log disponibile.'


def format_diagnosis(snapshot, record):
    response = record.get('response') or {}
    decision = response.get('decision') or {}
    final = record.get('final') or {}
    if not decision:
        return ('🧠 Diagnosi locale\n\nCampagna: ' + str(snapshot.get('campaign', '—')) +
                '\nModello: ' + str(response.get('status', 'non disponibile')) +
                '\nNessuna diagnosi valida. Decisione sicura: ' + str(final.get('action', 'continue')) + '.')
    labels = {
        'normal': 'normale', 'recoverable_error': 'errore recuperabile',
        'unrecoverable_error': 'errore non recuperabile', 'suspected_stall': 'possibile stallo',
        'unknown': 'incerta', 'continue': 'continua', 'request_human_review': 'richiedi verifica umana',
    }
    confidence = round(float(decision.get('confidence', 0)) * 100)
    return ('🧠 Diagnosi locale\n\nCampagna: ' + str(snapshot.get('campaign', '—')) +
            '\nValutazione: ' + labels.get(decision.get('classification'), str(decision.get('classification'))) +
            '\nConfidenza: ' + str(confidence) + '%\nAzione proposta: ' +
            labels.get(decision.get('action'), str(decision.get('action'))) +
            '\n\nMotivazione\n' + clean(decision.get('reason', '—')) +
            '\n\nDecisione applicata: nessuna azione automatica.')[:1850]


def format_perf_request(request, reason=None):
    lines = ['⚠️ Conflitto placement perf', '',
             'CPU perf configurate: ' + ', '.join(map(str, request['configured'])),
             'CPU online: ' + ', '.join(map(str, request['online']))]
    for row in request['conflicts']:
        lines.append(f"CPU{row['core']} {row['scenario']}: interferenti {row['interferers'] or 'nessuno'}, "
                     f"collisioni {row['collision'] or 'nessuna'}, offline {row['offline'] or 'nessuna'}, "
                     f"libere {row['free'] or 'nessuna'}")
    lines += ['', reason or ('Scegli se spostare i frontend perf sui core liberi, consentire la condivisione '
                             'con i workload oppure fermare la campagna. La CPU vittima resta separata.'),
              '', 'Senza risposta entro 10 minuti verranno usati i core liberi, se validi per tutta la campagna; altrimenti il test non partirà.']
    return '\n'.join(lines)[:1850]


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **data):
        request = urllib.request.Request(
            'https://api.telegram.org/bot' + self.token + '/' + method,
            data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                result = json.load(response)
            if not result.get('ok'):
                raise RuntimeError('Telegram API rejected request')
            return result['result']
        except Exception as exc:
            # HTTP exceptions may contain the token-bearing URL. Never expose it.
            raise RuntimeError('Telegram non disponibile (' + type(exc).__name__ + ')') from None

    def send(self, chat, text, **kwargs):
        # Telegram counts UTF-16 units; this bound also fits text containing emoji.
        return self.call('sendMessage', chat_id=chat, text=str(text)[:1900], **kwargs)


REMOTE = r'''
import json, os, signal, sys
from pathlib import Path
from jetson_tests.worker import checked_root, status
from jetson_tests.common import read_json
from jetson_tests.processes import boot_id, matches
root, relative, operation = sys.argv[1:]
root = checked_root(root)
out = (root / 'results' / relative).resolve()
if root / 'results' not in out.parents:
    raise ValueError('Invalid attempt')
snapshot = status(root, relative)
if operation == 'stop':
    launch = read_json(out / 'launch.json', {})
    identity = launch.get('identity')
    if snapshot['alive'] and identity and launch.get('boot_id') == boot_id() and matches(identity):
        os.kill(identity['pid'], signal.SIGTERM)
    print(json.dumps(snapshot))
else:
    snapshot['heartbeat'] = read_json(out / 'heartbeat.json', {})
    snapshot['failure'] = read_json(out / 'failure.json', {})
    snapshot['logs'] = []
    passes = sorted(out.glob('pass_*'), key=lambda p: p.stat().st_mtime)
    if passes:
        files = sorted(passes[-1].glob('*.stderr.txt')) + sorted(passes[-1].glob('*.stdout.txt'))
        for p in files:
            if p.stat().st_size:
                with p.open('rb') as stream:
                    stream.seek(max(0, p.stat().st_size - 800))
                    snapshot['logs'].append({'file': str(p.relative_to(out)), 'tail': stream.read(800).decode(errors='replace')})
                if len(snapshot['logs']) == 10:
                    break
    print(json.dumps(snapshot))
'''


class Control:
    def __init__(self, campaign):
        self.out = Path(campaign).resolve()
        if not (self.out / 'campaign.json').is_file():
            raise ValueError('Campaign directory not found')
        self.directory = self.out / 'telegram'
        self.directory.mkdir(exist_ok=True)
        self.transport = Transport(self.directory / 'transport')
        self.child = None

    def snapshot(self, operation='status'):
        ledger = read_json(self.out / 'campaign.json')
        attempts = ledger['attempts']
        if not attempts:
            result = {'campaign': ledger['id'], 'state': {'phase': 'NOT_STARTED'}, 'alive': False, 'logs': []}
            result['progress'] = campaign_progress(self.out, ledger, result)
            return result
        attempt = next((a for a in attempts if not a.get('download_verified')), attempts[-1])
        argv = ['python3', '-c', REMOTE, ledger['remote'], attempt['relative'], operation]
        _, data, _ = self.transport.ssh('cd ' + shlex.quote(ledger['remote'] + '/code') + ' && ' + shlex.join(argv), timeout=20)
        result = json.loads(data)
        result.update(campaign=ledger['id'], attempt=attempt['relative'],
                      downloaded=attempt.get('download_verified', False))
        result['progress'] = campaign_progress(self.out, ledger, result)
        save_json(self.directory / 'last-snapshot.json', result)
        return result

    def resume(self, restart=False):
        # Check compatibility BEFORE interrupting anything; Telegram is outside the code hash.
        if read_json(self.out / 'provenance.json')['code_hash'] != provenance()['code_hash']:
            return 'Codice cambiato: usa la revisione originale. Nessun processo interrotto.'
        plan = read_json(self.out / 'plan.json', {}).get('experiments', [])
        if not plan:
            return 'Piano assente: completa prima il preflight dal terminale.'
        if self.child and self.child.poll() is None:
            if not restart:
                return 'Controller già attivo.'
            self.child.send_signal(signal.SIGINT)
            self.child.wait(timeout=25)
        with (self.out / '.controller.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'Controller attivo in un altro terminale. Premi Ctrl-C lì, poi /resume qui. Il worker remoto conserva i timeout.'
            if restart:
                self.snapshot('stop')  # SIGTERM only to the identified campaign worker.
        args = [sys.executable, '-m', 'jetson_tests', 'resume', '--campaign', str(self.out), '--rerun-failed']
        # Preserve filters of the original run, not every scenario in its config.
        for key in ('core', 'scenario'):
            values = {row[key] for row in plan}
            if len(values) == 1:
                args += ['--' + key, str(next(iter(values)))]
        env = {k: v for k, v in os.environ.items() if k != 'TELEGRAM_BOT_TOKEN'}
        with (self.directory / 'controller.log').open('ab') as log:
            self.child = subprocess.Popen(args, cwd=Path(__file__).resolve().parent,
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                          start_new_session=True, env=env)
        return 'Controller avviato. Recupera gli artefatti e riprende senza ripetere i PASS. Usa /status.'

    def perf_notification(self):
        request = read_json(self.directory / 'perf-placement-request.json')
        if not request:
            return None
        decision = read_json(self.directory / 'perf-placement-decision.json', {})
        notified = read_json(self.directory / 'perf-placement-notified.json', {})
        if decision.get('request_id') == request['request_id'] or notified.get('request_id') == request['request_id']:
            return None
        config = read_json(self.out / 'config.json')['llm']
        llm_request = LLMRequest('perf-' + request['request_id'], 'perf_placement_conflict', request,
                                 ['request_human_review'])
        decide(config, llm_request, self.directory / 'llm', retries_left=0)
        record = read_json(self.directory / 'llm' / ('perf-' + request['request_id'] + '.json'), {})
        reason = ((record.get('response') or {}).get('decision') or {}).get('reason')
        labels = {'fallback': 'Usa core liberi', 'collision': 'Consenti collisione', 'stop': 'Ferma campagna'}
        keyboard = {'inline_keyboard': [[{'text': labels[action],
                                          'callback_data': f"perf:{request['request_id']}:{action}"}]
                                        for action in request['options']]}
        return request, format_perf_request(request, reason), keyboard

    def handle_callback(self, data):
        try:
            prefix, request_id, action = data.split(':', 2)
        except ValueError:
            return 'Risposta non valida.'
        request = read_json(self.directory / 'perf-placement-request.json', {})
        if prefix != 'perf' or request.get('request_id') != request_id or action not in request.get('options', []):
            return 'Richiesta scaduta o opzione non valida.'
        previous = read_json(self.directory / 'perf-placement-decision.json', {})
        if previous.get('request_id') == request_id:
            return 'La richiesta è già stata decisa: ' + str(previous.get('action')) + '.'
        save_json(self.directory / 'perf-placement-decision.json',
                  {'request_id': request_id, 'action': action, 'timestamp': time.time()})
        return {'fallback': 'Userò i core liberi.', 'collision': 'Collisione autorizzata per questa campagna.',
                'stop': 'Campagna fermata prima del test.'}[action]

    def handle(self, text, update_id):
        command = text.strip().split(maxsplit=1)[0].split('@')[0] if text.strip() else ''
        if command in ('/start', '/help'):
            return HELP
        if command in ('/resume', '/restart'):
            if len(text.strip().split()) != 1:
                return 'Il comando non accetta argomenti.'
            return self.resume(command == '/restart')
        if command not in ('/status', '/logs', '/diagnose'):
            return HELP
        snapshot = self.snapshot()
        if command == '/status':
            return format_status(snapshot)
        if command == '/logs':
            return format_logs(snapshot)
        config = read_json(self.out / 'config.json')['llm']
        context = {'state': snapshot.get('state', {}), 'failure': snapshot.get('failure', {}),
                   'logs': snapshot.get('logs', [])}
        request = LLMRequest(str(update_id), 'error_classification', context, ['continue', 'request_human_review'])
        decide(config, request, self.directory / 'llm', retries_left=0)
        record = read_json(self.directory / 'llm' / (str(update_id) + '.json'))
        return format_diagnosis(snapshot, record)


def authorized(update, chat, since):
    message = update.get('message', {})
    return (message.get('chat', {}).get('type') == 'private'
            and message['chat']['id'] == chat
            and message.get('from', {}).get('id') == chat
            and message.get('date', 0) >= since
            and isinstance(message.get('text'), str))


def authorized_callback(update, chat):
    query = update.get('callback_query', {})
    message = query.get('message', {})
    return (query.get('from', {}).get('id') == chat
            and message.get('chat', {}).get('id') == chat
            and message.get('chat', {}).get('type') == 'private'
            and isinstance(query.get('data'), str))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, help='Override: usa sempre questa campagna')
    parser.add_argument('--results-dir', type=Path, default=REPOSITORY / 'results',
                        help='Directory in cui selezionare automaticamente la campagna più recente')
    parser.add_argument('--pair', action='store_true', help='Associate your private chat using a local one-time code')
    args = parser.parse_args()
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        parser.error('Imposta TELEGRAM_BOT_TOKEN nel terminale; non inserirlo in YAML o Git.')
    api = Telegram(token)
    controls = {}

    def current_control():
        campaign = args.campaign.resolve() if args.campaign else latest_campaign(args.results_dir)
        if campaign not in controls:
            controls[campaign] = Control(campaign)
        return controls[campaign]
    state_file = Path(__file__).resolve().parent / '.telegram-state.json'
    with state_file.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read_json(state_file, {})
        bot_id = api.call('getMe')['id']
        if state.get('bot_id') != bot_id:
            state = {'bot_id': bot_id}
        code = secrets.token_hex(4) if args.pair else None
        if code:
            print('Invia al bot in chat privata: /pair ' + code, flush=True)
        elif not state.get('chat'):
            parser.error('Primo avvio: aggiungi --pair.')
        since = int(time.time())
        pair_deadline = time.monotonic() + 300
        print('Bot in ascolto. Nessuna run viene avviata automaticamente.', flush=True)
        while True:
            try:
                if code and time.monotonic() > pair_deadline:
                    raise SystemExit('Abbinamento scaduto; ripeti --pair.')
                updates = api.call('getUpdates', offset=state.get('offset', 0), timeout=20,
                                   allowed_updates=['message', 'callback_query'])
                for update in updates:
                    # Durable at-most-once handling: never replay a restart after a crash/send failure.
                    state['offset'] = update['update_id'] + 1
                    save_json(state_file, state)
                    message = update.get('message', {})
                    chat = message.get('chat', {}).get('id')
                    if not code and authorized_callback(update, state['chat']):
                        query = update['callback_query']
                        try:
                            control = current_control()
                            response = control.handle_callback(query['data'])
                            append_json(control.directory / 'commands.jsonl',
                                        {'update_id': update['update_id'], 'callback': query['data'], 'received': time.time()})
                            api.call('answerCallbackQuery', callback_query_id=query['id'], text=response)
                            if query.get('message', {}).get('message_id'):
                                api.call('editMessageReplyMarkup', chat_id=state['chat'],
                                         message_id=query['message']['message_id'],
                                         reply_markup={'inline_keyboard': []})
                        except Exception as exc:
                            api.call('answerCallbackQuery', callback_query_id=query['id'],
                                     text=('Operazione non completata: ' + str(exc))[:180], show_alert=True)
                        continue
                    if code:
                        if authorized(update, chat, since) and message['text'] == '/pair ' + code:
                            state['chat'] = chat
                            save_json(state_file, state)
                            code = None
                            api.send(chat, 'Chat abbinata.\n' + HELP)
                        continue
                    if not authorized(update, state['chat'], since):
                        continue
                    command = message['text']
                    try:
                        control = current_control()
                        append_json(control.directory / 'commands.jsonl', {'update_id': update['update_id'], 'command': command, 'received': time.time()})
                        response = control.handle(command, update['update_id'])
                    except Exception as exc:
                        response = 'Operazione non completata: ' + str(exc).replace(token, '[redacted]')[:1400]
                    api.send(chat, response)
                for control in controls.values():
                    if control.child and control.child.poll() is not None:
                        code_result = control.child.returncode
                        control.child = None
                        api.send(state['chat'], f'Controller terminato per {control.out.name}, exit code {code_result}. Usa /status e consulta telegram/controller.log.')
                if not code:
                    try:
                        control = current_control()
                        pending = control.perf_notification()
                        if pending:
                            request, text, keyboard = pending
                            message = api.send(state['chat'], text, reply_markup=keyboard)
                            save_json(control.directory / 'perf-placement-notified.json',
                                      {'request_id': request['request_id'], 'message_id': message.get('message_id'),
                                       'timestamp': time.time()})
                    except (ValueError, FileNotFoundError):
                        pass
            except RuntimeError as exc:
                print(str(exc).replace(token, '[redacted]'), file=sys.stderr, flush=True)
                time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Bot fermato. Controller e worker già avviati conservano i propri timeout.')
