# Misura successiva: quanto bilanciamento spiega instructions:k?

> Esito della prima coppia, `newidle-instructions.CLdsm2`: perf stat conferma
> +80,28% instructions:k, ma perf record non campiona il bilanciamento e mostra
> IP campionati in spazio utente nonostante exclude_user=1. La concentrazione
> allo sblocco IRQ e al cambio di contesto è compatibile con campionamento
> ritardato. Questa procedura non ha quindi fornito una quota attendibile del
> delta attribuibile alle funzioni. Non usare le percentuali del report per
> quella stima. Il seguito consigliato è una lettura PMU ai confini della
> funzione, da implementare e validare; dettagli in
> `newidle-instructions.CLdsm2/ANALISI.md`.

La coppia verificata `overload-origin.ugrjoc-repeat` identifica il meccanismo:
code con nr_running > 1, soprattutto CPU 1–3, producono overload nel dominio
condiviso con CPU 0. Il worker cyclictest lo legge a 1 con demo e procede verso
il bilanciamento. La prima acquisizione aveva già mostrato più load_balance
nel contesto del worker. Manca l'attribuzione quantitativa delle istruzioni.

Questa misura usa contatori e campioni PMU; non attivare i probe precedenti.
Il profilo comprende esclusivamente il TID FIFO 90, non il thread di gestione.
Verificati sulla Jetson: perf 5.15.209, opzioni -t/-c/--call-graph/-i,
CONFIG_FRAME_POINTER=y. Non ancora eseguita la misura PMU di questa procedura.
CONFIG_FTRACE_SYSCALLS è disabilitato: non richiedere il tracepoint
syscalls:sys_enter_clock_nanosleep come contatore.

## Preparazione sulla Jetson

```bash
TID=31815  # verificare che il worker sia ancora quello
ps -eLo pid,tid,cls,rtprio,psr,comm | grep '[c]yclictest'
PMU=$(mktemp -d /home/nvidia/codex-work/newidle-instructions.XXXXXX)
VMLINUX=/home/nvidia/codex-work/newidle-path-preparation/vmlinux
```

Mantieni cyclictest e parametri CEM identici alle acquisizioni precedenti.
Se il gruppo overload_origin è ancora registrato, rimuovilo dopo avere terminato
le catture che lo usano:

```bash
sudo bash /home/nvidia/codex-work/overload-origin-preparation/overload-events.sh remove
```

Non lasciare in funzione altre registrazioni ftrace/bpftrace/perf.

## Baseline

Con CEM effettivamente ferma e sistema a regime:

```bash
cat /proc/$TID/status > "$PMU/baseline-before.status"
ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$PMU/baseline.tasks"
sudo taskset -c 4,5 perf stat --no-inherit -t "$TID" \
  -e '{instructions:k,cycles:k}' -e context-switches \
  -o "$PMU/baseline-stat.txt" -- sleep 15
cat /proc/$TID/status > "$PMU/baseline-after.status"

sudo taskset -c 4,5 perf record --no-inherit -t "$TID" \
  -e instructions:k -c 100000 --call-graph fp \
  -o "$PMU/baseline.data" -- sleep 15 2> "$PMU/baseline-record.txt"
sudo perf report --stdio --children --percent-limit 0.5 \
  --vmlinux "$VMLINUX" -i "$PMU/baseline.data" > "$PMU/baseline-report.txt"
```

## Demo

Avvia CEM e il bag con gli stessi parametri del test precedente; aspetta
che il flusso sia operativo. Lascia acceso lo stesso cyclictest:

```bash
cat /proc/$TID/status > "$PMU/demo-before.status"
ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$PMU/demo.tasks"
sudo taskset -c 4,5 perf stat --no-inherit -t "$TID" \
  -e '{instructions:k,cycles:k}' -e context-switches \
  -o "$PMU/demo-stat.txt" -- sleep 15
cat /proc/$TID/status > "$PMU/demo-after.status"

sudo taskset -c 4,5 perf record --no-inherit -t "$TID" \
  -e instructions:k -c 100000 --call-graph fp \
  -o "$PMU/demo.data" -- sleep 15 2> "$PMU/demo-record.txt"
sudo perf report --stdio --children --percent-limit 0.5 \
  --vmlinux "$VMLINUX" -i "$PMU/demo.data" > "$PMU/demo-report.txt"
sudo chown nvidia:nvidia "$PMU/baseline.data" "$PMU/demo.data" \
  "$PMU/baseline-stat.txt" "$PMU/demo-stat.txt"
printf '%s\n' "$PMU"
```

## Criterio di interpretazione

1. I contatori devono essere disponibili e il gruppo hardware deve avere una
   quota di tempo di esecuzione vicina al 100%, senza forte multiplexing.
   Confrontare instructions:k per secondo e stabilità del numero di cicli.
   I cambi di contesto volontari da /proc sono un controllo del ritmo, non
   un conteggio esatto delle chiamate nanosleep.
2. Nel profilo a periodo fisso, confrontare i campioni kernel il cui stack
   contiene newidle_balance/load_balance e le sue funzioni discendenti.
   Contare ogni campione una sola volta: le percentuali Children dei genitori
   e dei figli si sovrappongono e non devono essere sommate.
3. Una quota inclusiva f del profilo, moltiplicata per instructions:k al secondo
   del perf stat nella stessa condizione, dà una stima delle istruzioni/s
   associate al percorso. La differenza demo-baseline si confronta con
   l'incremento totale. Sono finestre separate: è necessario un carico stabile.
4. Se i campioni sono pochi, gli stack incompleti o ci sono perdite/throttling,
   la stima va rifatta prima di quantificare la quota. Il campionamento ARM64
   può avere skid: non interpretare il singolo indirizzo come istruzione esatta
   responsabile dell'evento. Uno stack completo serve ad attribuire il percorso.

Una prima coppia serve a verificare acquisizione e simboli. Per una conclusione
quantitativa ripetere almeno tre coppie, alternando l'ordine delle condizioni,
con nuovi nomi file. Se il percorso spiega solo una parte del delta, analizzare
le altre famiglie che aumentano nel profilo, anziché assumere che tutto il delta
sia dovuto al bilanciamento.

Riferimenti delle opzioni:
[perf record](https://github.com/torvalds/linux/blob/v5.15/tools/perf/Documentation/perf-record.txt),
[perf stat](https://github.com/torvalds/linux/blob/v5.15/tools/perf/Documentation/perf-stat.txt).
