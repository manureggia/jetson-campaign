# Seconda acquisizione: chi produce overload?

La prima coppia (`newidle-branches.1b6flI`) ha mostrato che il worker supera i
controlli iniziali in 1/4952 invocazioni baseline e 3327/4920 con demo.
Una volta entrato nel ramo, esegue quasi sempre due load_balance. Il cambiamento
principale osservato è quindi il flag overload, non un aumento del numero di
domini visitati per ingresso. La seconda acquisizione cerca chi lo produce.

Il flag è condiviso dal root_domain: può cambiare per una coda diversa da CPU 0.
Due produttori nel codice fornito:

* `sched.h:2399`: add_nr_running imposta overload a 1 quando nr_running passa da
  meno di 2 ad almeno 2, se il flag è ancora zero. Nel binario sono coperti i
  setter FAIR, RT, DEADLINE e STOP.
* `fair.c:9687`: la scansione del dominio superiore riscrive il flag con il
  risultato SG_OVERLOAD. I contributi sono nr_running > 1 (`fair.c:9029`) e
  misfit_task_load (`fair.c:9051`). Non richiede che ogni CPU sia occupata.

Il percorso unthrottle_cfs_rq che contiene un altro add_nr_running nel sorgente
non è presente in questa build: CONFIG_FAIR_GROUP_SCHED è disabilitato.
Le sonde sono per la build ID `40157ac384cbddc13766cb58d82594769fbd09fb`, già
confrontata con le note della Jetson. Non usarle con un altro kernel.

L’ingresso comune `enqueue_task` è stato rifiutato dal kernel durante setup.
L’identificazione usa ora ingresso e ritorno delle quattro funzioni
`enqueue_task_fair`, `enqueue_task_rt`, `enqueue_task_dl`, `enqueue_task_stop`.
Il setup crea 19 eventi; i setter e la lettura del flag restano quelli verificati.

## Comandi sulla Jetson

Gli script sono in `/home/nvidia/codex-work/overload-origin-preparation`.
Conserva l'assetto del test precedente: worker FIFO 90 su CPU 0, stesso intervallo,
stessi parametri CEM. Arresta le altre acquisizioni perf/ftrace e mantieni
cyclictest acceso durante entrambe le catture. Le vecchie sonde disabilitate
possono restare registrate: i nuovi eventi hanno nomi distinti `ov_*`.

```bash
K=/home/nvidia/codex-work/overload-origin-preparation
OUT=$(mktemp -d /home/nvidia/codex-work/overload-origin.XXXXXX)
ps -eLo pid,tid,cls,rtprio,psr,comm | grep '[c]yclictest'
```

Imposta TID al worker FF 90, non al processo principale. Il vecchio TID 31815
vale soltanto se quel worker è ancora acceso:

```bash
TID=31815  # sostituire se il worker è cambiato
sudo bash "$K/overload-events.sh" setup
```

Con CEM spenta e cyclictest a regime:

```bash
sudo taskset -c 4,5 bash "$K/overload-events.sh" record "$TID" "$OUT/baseline"
python3 "$K/analyze-overload.py" "$OUT/baseline.trace" | tee "$OUT/baseline-summary.txt"
```

Avvia CEM con gli stessi parametri del test precedente e aspetta che sia operativa:

```bash
sudo taskset -c 4,5 bash "$K/overload-events.sh" record "$TID" "$OUT/demo"
python3 "$K/analyze-overload.py" "$OUT/demo.trace" | tee "$OUT/demo-summary.txt"
sudo bash "$K/overload-events.sh" remove
printf '%s\n' "$OUT"
```

Ogni cattura dura cinque secondi e usa un'istanza dedicata con tutte le CPU online,
buffer di 16 MiB per CPU e clock mono. Solo reader e nanosleep sono filtrati per
TID; filtrare anche gli scrittori perderebbe le cause prodotte dalle altre CPU.
L'istanza viene rimossa alla fine. Non vengono modificati i parametri scheduler.

Conserva tutti i file. Se setup o analisi falliscono, conserva l'errore e non
interpretare conteggi parziali. L'analizzatore verifica gli overrun di ogni CPU e
i miss kprobe/kretprobe. I file tasks-before/after aiutano a collegare comm e TID
ai comandi effettivi; comm da solo può essere abbreviato.

## Lettura del riepilogo

* `reads`: `(rd, valore)` letto dal worker. Verificare che il confronto riproduca
  il cambiamento del flag visto nella prima acquisizione. Le letture avvengono
  solo dopo il superamento della soglia idle: non sono tutti i cicli.
* `writers`: `(rd, evento, valore scritto)` per gli stessi domini del worker.
  fair_set/rt_set/dl_set/stop_set sono scritture di 1; scan_write può scrivere
  0 oppure 1. Contano anche le riscritture dello stesso valore.
* `causes`: `(rd, valore scritto dalla scansione, tipo, CPU della coda,
  nr_running oppure misfit, comm corrente, TID corrente)`. Una scansione può
  avere più contributi; non sono tutti task CEM né tutti aggiornamenti distinti.
* `tasks`: `(rd, setter, CPU della coda, comm del task aggiunto, TID, TGID,
  policy, comm corrente, TID corrente)`. Il task aggiunto è associato soltanto
  se la funzione enqueue_task della stessa classe è ancora attiva per quella coda. NON_ASSOCIATO rimane tale;
  per esempio un helper RT può essere chiamato da altri percorsi.

Il campione curr non è l'elenco della coda e non prova da solo quali due task
siano contemporaneamente eseguibili. I probe dei setter sono prima della store:
old_snapshot è una lettura separata e non un precedente valore atomico.
Gli eventi su CPU diverse non stabiliscono quale singola store abbia prodotto
una singola lettura. Il riepilogo identifica produttori e condizioni osservate,
non presenta un abbinamento temporale come prova causale.
Le coppie ingresso/ritorno possono essere tagliate ai margini della finestra;
una cattura con miss o buffer perduti va ripetuta.

L'esito utile è capire se con CEM una particolare CPU/coda e determinati task
innescano i setter, e se le scansioni confermano overload=1 più spesso nello
stesso dominio. Se prevale misfit, la spiegazione riguarda il rapporto tra
capacità CPU e carico stimato: non soltanto il numero di task.

## Collegamento alle istruzioni kernel

Questa acquisizione identifica i produttori del flag; non misura istruzioni.
Dopo aver chiarito l'origine, fare una coppia separata senza questi probe:
`perf stat` sul medesimo worker con `instructions:k` e una coppia `perf record`
con call graph, conservando tempi, cicli e conteggi nanosleep confrontabili.
Le sonde aggiungono lavoro nello scheduler, quindi non utilizzare il contatore
istruzioni raccolto durante questa cattura per quantificare il fenomeno iniziale.
La catena da verificare è: task/coda → overload → più ingressi nel ramo →
istruzioni attribuite al worker dentro quel percorso. L'ultima parte resta da
misurare; i risultati della prima coppia non ne quantificano ancora la quota.

## Riferimenti dei probe

Offset decimali, ricavati dal disassemblato locale in
`diagnostics/newidle-path-preparation/`:

| Funzione | Offset | Punto |
|---|---:|---|
| find_busiest_group | 428 | csel dopo nr_running > 1 |
| find_busiest_group | 648 | ramo che contribuisce misfit |
| find_busiest_group | 2684 | store di SG_OVERLOAD |
| enqueue_task_fair | 632 | store overload=1 |
| enqueue_top_rt_rq | 204 | store overload=1 |
| enqueue_task_dl | 956 | store overload=1 |
| enqueue_task_stop | 116 | store overload=1 |
| newidle_balance.constprop.0 | 156 | operando overload già caricato |

rq: nr_running+4, curr+2312, rd+2400, cpu+2520; rd: overload+88.
Layout task_struct fornito da gdb sullo stesso vmlinux: pid+1424, tgid+1428,
comm+1928, policy+960. Non sono offset generici per Linux/Jetson.
Sintassi fetcharg e contatori miss:
[documentazione kernel](https://www.kernel.org/doc/html/v5.15/trace/kprobetrace.html).

Verifica locale: `bash -n tools/overload-events.sh` e
`python3 tests/check_overload_trace.py`. La verifica con acquisizione reale
richiede sudo; la sola verifica sintattica non prova l'aggancio dei probe.
