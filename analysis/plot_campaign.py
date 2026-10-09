"""Rebuild the graphical report from verified campaign artifacts (matplotlib only)."""
import argparse
import csv
import html
import json
from pathlib import Path
import statistics
import sys
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

from jetson_tests.common import read_json, save_json, file_hash
from jetson_tests.report import selected_attempts
from jetson_tests.transport import verify_download

SCENARIOS = ['baseline', 'interfgen', 'demo']
COLORS = {0: '#2463A8', 3: '#E47B35'}
SCOPE = {'victim_cpu': 'CPU vittima', 'victim_task': 'Task cyclictest', 'interferer': 'Interferenti'}
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11, 'axes.titlesize': 14,
                     'axes.labelsize': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                     'axes.edgecolor': '#ADB7C3', 'text.color': '#18283C',
                     'axes.labelcolor': '#34475A', 'xtick.color': '#34475A', 'ytick.color': '#34475A',
                     'svg.fonttype': 'none', 'savefig.facecolor': 'white'})


def load(campaign):
    chosen = selected_attempts(read_json(campaign / 'campaign.json')['attempts'])
    entries = []
    for attempt in chosen:
        if attempt['item']['kind'] != 'measurement' or not attempt.get('download_verified'):
            continue
        directory = campaign / 'results' / attempt['relative']
        verify_download(directory)
        passes = {}
        for path in directory.glob('pass_*/metrics.json'):
            data = read_json(path)
            if not data['issues'] and data.get('latency') and data['window'].get('fifo_observed'):
                passes[path.parent.name.removeprefix('pass_')] = data
        entries.append(dict(attempt=attempt, passes=passes, directory=directory))
    return entries


def latency(field, reduction='mean'):
    return ('latency', 'core', field, reduction)


def perf(scope, pass_name, event):
    return ('perf', pass_name, scope, event)


def ratio(scope, pass_name, numerator, denominator, factor=100):
    return ('ratio', pass_name, scope, numerator, denominator, factor)


def resolve_pass_name(entry, name):
    # Historical expressions use task_*; new campaigns write victim_*.
    if name not in entry['passes'] and name.startswith('task_'):
        return 'victim_' + name.removeprefix('task_')
    return name


def event_keys(metrics, key):
    if ':' not in key and any(key + ':' + mode in metrics for mode in ('u', 'k')):
        return [key + ':u', key + ':k']
    return [key]


def expression_pass_name(entry, expression):
    kind, name, *args = expression
    name = resolve_pass_name(entry, name)
    if kind not in ('perf', 'ratio'):
        return name
    scope, *events = args if kind == 'perf' else args[:-1]
    # Configured campaigns can move events into combined or custom passes.
    for candidate in dict.fromkeys([name, *entry['passes']]):
        metrics = entry['passes'].get(candidate, {}).get('perf', {}).get(scope, {})
        if all(any(k in metrics for k in event_keys(metrics, event.partition(':')[0])) for event in events):
            return candidate
    return name


def value(entry, expression):
    kind, pass_name, *args = expression
    pass_name = expression_pass_name(entry, expression)
    measured = entry['passes'].get(pass_name)
    if measured is None:
        return None
    if kind == 'latency':
        return measured['latency'].get(args[0])
    if kind in ('perf', 'ratio'):
        scope = measured.get('perf', {}).get(args[0], {})
        def event(key):
            records = [scope.get(k, {}) for k in event_keys(scope, key)]
            if any(r.get('status') != 'ok' or r.get('value') is None for r in records):
                return None
            return sum(r['value'] for r in records)
        numerator = event(args[1])
        if kind == 'perf':
            return numerator
        denominator = event(args[2])
        return (numerator, denominator) if numerator is not None and denominator is not None and denominator > 0 else None
    if kind == 'irq':
        return measured.get('interrupts', {}).get(args[0])
    if kind == 'irq_kind':
        path = entry['directory'] / 'pass_core/interrupts_delta.csv'
        if not path.exists():
            return None
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        cpu = 'CPU' + str(entry['attempt']['item']['core'])
        return sum(int(r['delta']) for r in rows if r['cpu'] == cpu and r['kind'] == args[0])
    raise ValueError(expression)


def summarize(entries, expression, core, scenario, partial=False):
    selected = []
    for entry in entries:
        a = entry['attempt']
        if a['item']['core'] != core or a['item']['scenario'] != scenario:
            continue
        if a['outcome'] != 'PASS' and not partial:
            continue
        v = value(entry, expression)
        if v is not None:
            selected.append((entry, v))
    if not selected:
        return None, [], []
    values = [v for _, v in selected]
    if expression[0] == 'ratio':
        factor = expression[-1]
        mean = sum(v[0] for v in values) / sum(v[1] for v in values) * factor
        points = [n / d * factor for n, d in values]
    else:
        reduction = expression[3] if expression[0] == 'latency' else 'mean'
        mean = {'min': min, 'max': max, 'mean': statistics.mean}[reduction](values)
        points = values
    sources = []
    for entry, v in selected:
        pass_name = expression_pass_name(entry, expression)
        source = {'attempt': entry['attempt']['relative'], 'status': entry['attempt']['outcome'],
                  'pass': pass_name, 'input': v}
        if expression[0] in ('perf', 'ratio'):
            metrics = entry['passes'][pass_name]['perf'][expression[2]]
            events = expression[3:4] if expression[0] == 'perf' else expression[3:5]
            source['components'] = {k: metrics[k]['value'] for event in events for k in event_keys(metrics, event)}
        sources.append(source)
    return mean, points, sources


def pretty(value):
    if value == 0:
        return '0'
    if abs(value) >= 1000:
        return f'{value:,.0f}'.replace(',', ' ')
    return f'{value:.3g}'


def panel(ax, entries, descriptor, partial, records, duration_s, cores):
    title, unit, expression = descriptor
    groups = {(scenario, cpu): summarize(entries, expression, cpu, scenario, partial)
              for scenario in SCENARIOS for cpu in cores}
    maximum = max((max([v, *points]) for v, points, _ in groups.values() if v is not None), default=1)
    scale, prefix = (1, '')
    if unit == 'conteggi':
        for candidate, label in [(1e9, 'miliardi'), (1e6, 'milioni'), (1e3, 'migliaia')]:
            if maximum >= candidate:
                scale, prefix = candidate, label
                break
    label = (f'{prefix or "conteggi"} / finestra di {duration_s:g} s') if unit == 'conteggi' else unit
    ax.set_title(title, loc='left', pad=14, fontweight='bold')
    ax.set_ylabel(label)
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#E5EAF0', linewidth=.8)
    labels = []
    for i, scenario in enumerate(SCENARIOS):
        counts = []
        for cpu, offset in zip(cores, np.linspace(-.2, .2, len(cores)) if len(cores) > 1 else [0]):
            aggregate, points, sources = groups[scenario, cpu]
            counts.append(len(points))
            x = i + offset
            records.append(dict(title=title, unit=unit, expression=expression, partial=partial,
                                scenario=scenario, core=cpu, value=aggregate, n=len(points), sources=sources))
            if aggregate is None:
                ax.text(x, .025, 'N/D', transform=ax.get_xaxis_transform(), ha='center', fontsize=9, color='#8492A3')
                continue
            ax.bar(x, aggregate / scale, width=.34, color=COLORS[cpu], alpha=.9, zorder=2)
            jitter = np.linspace(-.055, .055, len(points)) if len(points) > 1 else [0]
            for shift, point, source in zip(jitter, points, sources):
                ax.scatter(x+shift, point / scale, s=23, marker='D' if source['status'] != 'PASS' else 'o',
                           facecolors='white', edgecolors='#23354B', linewidths=.8, zorder=3)
            ax.annotate(pretty(aggregate / scale), (x, max([aggregate, *points]) / scale),
                        xytext=(0, 7), textcoords='offset points', ha='center', fontsize=10, color='#23354B')
        labels.append(scenario.upper() + '\nn=' + '/'.join(map(str, counts)))
    ax.set_xticks(range(3), labels, fontsize=10)
    ax.set_ylim(0, maximum / scale * 1.24 if maximum > 0 else 1)
    ax.ticklabel_format(axis='y', style='plain', useOffset=False)
    ax.set_xlim(-.6, 2.6)  # Keep N/D groups visible even when no bar sets their data limits.


def figure(entries, target, number, slug, title, descriptors, note, partial, records, duration_s, cores, pdf):
    n = len(descriptors)
    rows, cols = (1, n) if n <= 3 else (2, 2)
    fig, axes = plt.subplots(rows, cols, figsize=(14, 8.4), squeeze=False)
    fig.subplots_adjust(left=.07, right=.97, bottom=.22, top=.78, hspace=.72, wspace=.32)
    fig.text(.06, .944, 'JETSON ORIN  /  ' + ' vs '.join(f'CPU {c}' for c in cores), fontsize=11, color='#607186', weight='bold')
    fig.text(.06, .885, title, fontsize=23, weight='bold')
    tag = 'ESPLORATIVO - include passate valide di tentativi FAIL/INCOMPLETE' if partial else 'CONFRONTO PRINCIPALE - solo tentativi interamente PASS'
    fig.text(.06, .837, tag, fontsize=10, color='#9B531C' if partial else '#486279')
    fig.legend(handles=[Patch(color=COLORS[c], label=f'CPU vittima {c}') for c in cores],
               loc='upper right', bbox_to_anchor=(.965, .98), ncol=2, frameon=False, fontsize=11)
    for ax, descriptor in zip(axes.flat, descriptors):
        panel(ax, entries, descriptor, partial, records, duration_s, cores)
    for ax in list(axes.flat)[n:]:
        ax.set_visible(False)
    fig.text(.06, .079, note, fontsize=10, linespacing=1.35, color='#526275')
    fig.text(.06, .033, 'n = ripetizioni ' + ' / '.join(f'CPU {c}' for c in cores) + '. Cerchi = singole ripetizioni; rombi = passate da tentativi parziali.', fontsize=9, color='#728094')
    fig.text(.96, .033, f'{number:02d}', ha='right', fontsize=11, color='#728094')
    name = f'{number:02d}_{slug}'
    fig.savefig(target / (name + '.svg'))
    if pdf is not None:
        pdf.savefig(fig)
    plt.close(fig)
    return dict(name=name, title=title, partial=partial)


def build(campaign, pdf=False):
    entries = load(campaign)
    durations = {e['attempt']['item']['duration_s'] for e in entries}
    if len(durations) != 1 or next(iter(durations)) <= 0:
        raise ValueError('Servono acquisizioni verificate con una sola durata positiva; non si confrontano conteggi di finestre diverse.')
    target = campaign / 'report/grafici'
    target.mkdir(parents=True, exist_ok=True)
    document = PdfPages(target / 'confronto_core0_core3.pdf', metadata={'Title': 'Jetson Orin - confronto CPU vittime',
                        'Author': 'Campagna Jetson'}) if pdf else nullcontext()
    with document as output:
        return build_pages(campaign, entries, target, output, next(iter(durations)))


def build_pages(campaign, entries, target, pdf, duration_s):
    cores = sorted({e['attempt']['item']['core'] for e in entries})
    records, pages = [], []
    split_metrics = sorted({(scope, name, key[:-2]) for e in entries for name, measured in e['passes'].items()
                            for scope, metrics in measured.get('perf', {}).items() for key in metrics
                            if key.endswith((':u', ':k'))})
    represented = set()
    def add(slug, title, descriptors, note, partial=False):
        split = []
        for label, unit, expression in descriptors:
            if expression[0] != 'perf' or ':' in expression[3]:
                continue
            _, _, scope, event = expression
            measured_keys = {(scope, expression_pass_name(e, expression), event) for e in entries}
            represented.update(measured_keys)
            if measured_keys.intersection(split_metrics):
                split.extend((f'{label} - {mode}', unit, perf(scope, expression[1], event + ':' + modifier))
                             for mode, modifier in [('user (:u)', 'u'), ('kernel (:k)', 'k')])
        aggregate_note = '\nTotali = user + kernel nella stessa passata; se manca una componente, N/D.' if split else ''
        pages.append(figure(entries,target,len(pages)+1,slug,title,descriptors,note+aggregate_note,partial,records,duration_s,cores,pdf))
        for i in range(0, len(split), 4):
            pages.append(figure(entries,target,len(pages)+1,slug+f'_user_kernel_{i//4+1}',title+' - user / kernel',
                split[i:i+4], 'Conteggi user (:u) e kernel (:k) separati, della stessa passata e dello stesso scope.\nBarre = medie tra ripetizioni; componenti mancanti = N/D, mai zero.',partial,records,duration_s,cores,pdf))
    count_note = f'Barre = media aritmetica dei conteggi per ripetizione. Ogni metrica usa la propria passata di {duration_s:g} s.'
    ratio_note = 'Rapporto tra somme di eventi della stessa passata; punti = rapporti delle singole ripetizioni.\nStall memoria e backend non sono componenti da sommare.'
    latency_desc = [('Minimo dei minimi', 'microsecondi (µs)', latency('min_us','min')),
                    ('Media di tutti gli avg', 'microsecondi (µs)', latency('reported_mean_us')),
                    ('Massimo dei massimi', 'microsecondi (µs)', latency('max_us','max'))]
    add('latenza','Latenza cyclictest',latency_desc,
        'Solo passata core: minimo dei minimi, media aritmetica degli avg nel JSON di cyclictest, massimo dei massimi.\nLe altre passate e il profiling non vengono mescolati; n indica le ripetizioni disponibili per condizione.')
    stalls = [(f'{SCOPE[s]} - {label}', 'conteggi', perf(s,p,event))
              for s,p in [('victim_cpu','core'),('victim_task','task_core')]
              for label,event in [('stall backend','backend_stall'),('stall memoria','memory_stall')]]
    add('stall_conteggi','Backend stall e memory stall',stalls,count_note+'\nStall memoria e backend sono eventi distinti, non categorie additive.')
    stall_ratios = [(f'{SCOPE[s]} - {label}', '% dei cicli', ratio(s,p,event,'cycles'))
                    for s,p in [('victim_cpu','core'),('victim_task','task_core')]
                    for label,event in [('stall backend','backend_stall'),('stall memoria','memory_stall')]]
    add('stall_ratio','Stall normalizzati sui cicli',stall_ratios,ratio_note)
    add('istruzioni','Istruzioni ritirate e IPC',[
        ('Task cyclictest - istruzioni','conteggi',perf('victim_task','task_core','instructions')),
        ('CPU vittima - istruzioni','conteggi',perf('victim_cpu','core','instructions')),
        ('Task cyclictest - IPC','istruzioni / ciclo',ratio('victim_task','task_core','instructions','cycles',1)),
        ('CPU vittima - IPC','istruzioni / ciclo',ratio('victim_cpu','core','instructions','cycles',1))],
        'Task cyclictest include inizializzazione, thread e kernel nel contesto del task. CPU vittima include altri task e IRQ.\nIstruzioni: media dei conteggi. IPC: somma istruzioni / somma cicli, nella stessa passata.')
    memory = [(f'{SCOPE[s]} - {label}', 'conteggi',perf(s,p,event))
              for s,p in [('victim_task','task_memory'),('victim_cpu','memory')]
              for label,event in [('accessi memoria','memory_accesses'),('accessi bus','bus_accesses')]]
    add('memoria_bus','Accessi alla memoria e al bus',memory,
        count_note+'\nBUS_ACCESS conta beat tra core e SCU: non byte DRAM. MEM_ACCESS non equivale a traffico DRAM.')
    add('interferenti','Traffico dei processi interferenti',[
        ('Interferenti - accessi memoria','conteggi',perf('interferer','memory','memory_accesses')),
        ('Interferenti - accessi bus','conteggi',perf('interferer','memory','bus_accesses')),
        ('Interferenti - istruzioni','conteggi',perf('interferer','core','instructions')),
        ('Interferenti - stall memoria','% dei cicli',ratio('interferer','core','memory_stall','cycles'))],
        'BASELINE non ha processi interferenti: N/D, non zero. Aggregazione dei processi agganciati da perf.\nLo scope comprende i processi monitorati dal protocollo della campagna.')
    caches=[('l1d','L1 dati'),('l1i','L1 istruzioni + L0'),('l2','L2 unified'),('l3','L3 attribuibile al core')]
    for scope, prefix in [('victim_cpu',''),('victim_task','task_')]:
        for event,label in [('accesses','Accessi'),('refills','Refill')]:
            desc=[(name,'conteggi',perf(scope,prefix+cache,cache+'_'+event)) for cache,name in caches]
            add(scope+'_'+event, f'{SCOPE[scope]} - {label.lower()} alle cache',desc,
                count_note+'\nL1I comprende L0 macro-op; L3 è attribuibile al core. I livelli seguono le passate configurate.')
        desc=[(name,'% refill / accessi',ratio(scope,prefix+cache,cache+'_refills',cache+'_accesses')) for cache,name in caches]
        add(scope+'_refill_ratio',f'{SCOPE[scope]} - refill ratio',desc,
            '100 × somma refill / somma accessi dello stesso livello e della stessa passata; non una catena di miss end-to-end.\nL3 refill: risposte dati provenienti da fuori cluster; non il tasso di miss globale della cache condivisa.')
    add('memoria_normalizzata','Accessi normalizzati sui cicli',[
        (f'{SCOPE[s]} - {label}','eventi / 1.000 cicli',ratio(s,p,event,'cycles',1000))
        for s,p in [('victim_task','task_memory'),('victim_cpu','memory')]
        for label,event in [('memoria','memory_accesses'),('bus','bus_accesses')]],
        'Rapporti tra somme di eventi e cicli della stessa passata, individuata nelle acquisizioni.\nI livelli cache e gli scope task/CPU restano distinti.')
    add('interrupt','Interrupt durante la passata core',[
        ('Interrupt sul core vittima','conteggi',('irq','core','victim')),
        ('Interrupt su tutte le CPU','conteggi',('irq','core','total')),
        ('Vittima - timer','conteggi',('irq_kind','core','timer')),
        ('Vittima - IRQ hardware','conteggi',('irq_kind','core','hardware_irq'))],
        'Delta di /proc/interrupts prima/dopo la passata core, mediati tra ripetizioni. La finestra include il breve avvio/arresto.\nLe categorie seguono il parser del progetto; il totale include anche IPI ed eventi locali.')
    add('interrupt_ipi','Interrupt locali e distribuzione',[
        ('Vittima - scheduler IPI','conteggi',('irq_kind','core','scheduler_ipi')),
        ('Vittima - altri IPI / locali','conteggi',('irq_kind','core','ipi_or_local')),
        ('Vittima - altre voci locali','conteggi',('irq_kind','core','local_non_numeric'))],
        'Conteggi medi della passata core. Uno zero è una misura presente; N/D indica l’assenza di dati validi.')
    # Include split events from custom passes even when absent from the standard pages.
    for index, (scope, pass_name, event) in enumerate(split_metrics):
        if (scope, pass_name, event) not in represented:
            unit = 'ms' if event == 'task_clock' else 'conteggi'
            add(f'pmu_{index}_{scope}_{pass_name}_{event}', f'{SCOPE[scope]} - {event.replace("_", " ")}',
                [(f'{event} - totale', unit, perf(scope, pass_name, event))], count_note)
    add('parziali_latenza','Appendice - latenze con dati parziali',latency_desc,
        'Include solo passate core già validate, anche dentro tentativi FAIL/INCOMPLETE. Non sostituisce il confronto principale.\nUn solo tentativo per ripetizione: primo PASS se presente, altrimenti ultimo tentativo. Nessuna somma dei retry.',True)
    add('parziali_memoria','Appendice - memoria con dati parziali',memory,
        'Ogni pannello include solo passate già validate; n può cambiare tra metriche. Nessuna estrapolazione dei dati interrotti.\nLe passate interrotte o mancanti restano N/D; non si completano con dati di altri retry.',True)
    add('parziali_cache_ratio','Appendice - refill ratio CPU vittima',[
        (name,'% refill / accessi',ratio('victim_cpu',cache,cache+'_refills',cache+'_accesses')) for cache,name in caches],
        'Livelli cache mancanti o interrotti restano N/D.\nRombi = singole passate validate provenienti da tentativi non interamente PASS.',True)
    save_json(target/'plot-data.json',records)
    save_json(target/'pages.json',pages)
    save_json(target/'sources.json',{'campaign':campaign.name,'config_hash':read_json(campaign/'campaign.json')['config_hash'],
        'generator_sha256':file_hash(Path(__file__)), 'duration_s':duration_s,
        'attempts':[{'relative':e['attempt']['relative'],'status':e['attempt']['outcome'],
                     'manifest_sha256':file_hash(e['directory']/'manifest.json'),'valid_passes':sorted(e['passes'])} for e in entries]})
    core_label = ' / '.join(f'CPU {c}' for c in cores)
    introduction = (f'Confronto {core_label} per BASELINE, INTERFGEN e DEMO. '
        'Grafici principali: solo tentativi PASS. Appendice: passate valide di tentativi parziali, chiaramente separate.')
    markdown = [f'# Jetson Orin - {core_label}', '', introduction, '',
                f'Finestre nominali: {duration_s:g} s. Per gli eventi separati, totale = user + kernel nella stessa passata; componenti mancanti = N/D. Altrimenti si usa il contatore senza suffisso.', '',
                'Ogni metrica raccolta con :u / :k compare prima aggregata, poi separata. Barre = aggregati; punti = ripetizioni.', '']
    for page in pages:
        image_path = (target / (page['name'] + '.svg')).resolve()
        markdown.extend([f'## {page["title"]}', '', f'![{page["title"]}](<{image_path}>)', ''])
    (target / 'report.md').write_text('\n'.join(markdown))
    sections='\n'.join(f'<section id="{p["name"]}"><img src="{p["name"]}.svg" alt="{html.escape(p["title"])}" loading="lazy">'
        f'<p><a href="{p["name"]}.svg">SVG vettoriale</a></p></section>' for p in pages)
    pdf_link = '<a href="confronto_core0_core3.pdf">Scarica PDF</a> · ' if pdf is not None else ''
    (target/'index.html').write_text('<!doctype html><html lang="it"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Jetson - '+core_label+'</title><style>body{font-family:system-ui;margin:0;background:#edf1f5;color:#18283c}main{max-width:1250px;margin:auto;padding:28px}h1{font-size:30px}section{background:white;margin:24px 0;border-radius:12px;overflow:hidden}img{display:block;width:100%;height:auto}p{line-height:1.6}section p{padding:0 24px 12px}a{color:#2463a8}</style>'
        '<main><h1>Jetson Orin · '+core_label+'</h1><p>'+introduction+'</p><p>'+pdf_link+'<a href="report.md">Markdown</a></p>'+sections+'</main></html>')
    print(target)
    return target


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('campaign',type=Path)
    parser.add_argument('--pdf', action='store_true', help='Esporta anche il PDF vettoriale')
    args = parser.parse_args()
    build(args.campaign.resolve(), pdf=args.pdf)
