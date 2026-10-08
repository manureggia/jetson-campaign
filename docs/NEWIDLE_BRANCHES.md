# Prima acquisizione: perché newidle_balance procede al bilanciamento?

La procedura corrente usa direttamente eventi kprobe tracefs. Il pacchetto
bpftrace 0.14 installato sulla Jetson non supporta la verifica delle sonde a
Offset interno. Inoltre il kernel distingue gli eventi kprobe dinamici dai
tracepoint normali: non basta cambiare provider in bpftrace. I precedenti comandi
con `newidle-path.bt` sono quindi superati.

## Punti verificati

Kernel `5.15.148-rt-tegra`, build del 30 settembre 2026 14:12:26 CEST;
GNU build ID `40157ac384cbddc13766cb58d82594769fbd09fb`. Le note del vmlinux
fornito coincidono byte per byte con `/sys/kernel/notes` della Jetson.
Lo script controlla questa corrispondenza prima di preparare o registrare.
Non riutilizzare gli offset dopo un cambio di kernel.

Il sorgente fornito, fair.c:11294, contiene:

```c
if (this_rq->avg_idle < sysctl_sched_migration_cost ||
    !READ_ONCE(this_rq->rd->overload)) {
    /* ... */
    goto out;
}
```

Gli eventi leggono gli operandi già caricati dal kernel, nel punto in cui
esegue ogni salto condizionato. Non leggono strutture rq tramite layout ipotizzati.
Nel binario la soglia idle è incorporata come limite 499999 ns: il controllo
è `avg_idle <= 499999`, equivalente a `< 500000 ns`.

| Offset | Operazione | Valore osservato |
|---|---|---|
| 80 | cbnz w0 | ttwu_pending |
| 120 | tbz w0,#0 | cpu_active |
| 144 | b.ls | avg_idle e limite 499999 |
| 156 | cbnz w0 | overload, valutato solo dopo il controllo idle |
| 580 | b.cc | avg_idle e costo cumulato + costo del dominio |
| 588 | tbz w0,#0 | SD_BALANCE_NEWIDLE |

Vengono registrati anche ingresso/ritorno di newidle_balance, load_balance e
clock_nanosleep. Sintassi degli eventi:
[documentazione kernel 5.15](https://www.kernel.org/doc/html/v5.15/trace/kprobetrace.html).
I nomi nativi ARM64 per tracefs sono `%x0` e `%x1`.

## Preparazione

I due script sono già copiati sulla Jetson in
`/home/nvidia/codex-work/newidle-path-preparation/`. Per ricopiarli dal Mac:

```bash
scp tools/newidle-events.sh tools/analyze-newidle.py \
  jetson-codex:/home/nvidia/codex-work/newidle-path-preparation/
```

Sulla Jetson, nello stesso terminale dei tentativi precedenti:

```bash
K=/home/nvidia/codex-work/newidle-path-preparation
sudo bash "$K/newidle-events.sh" setup
```

Deve confermare il kernel e creare dieci eventi. Se fallisce, conserva l'errore
prima di proseguire. Lo script rimuove gli eventi appena creati se una definizione
fallisce; non cancella le altre sonde. Se il gruppo esiste già, setup si ferma:
usa `remove` solo dopo aver terminato acquisizioni che lo utilizzano.

## Prima coppia

Conserva OUT e TID già impostati. Verifica il worker:

```bash
ps -eLo pid,tid,cls,rtprio,psr,comm | grep '[c]yclictest'
```

Deve essere il worker FF 90 su CPU 0. Mantieni lo stesso assetto e cyclictest
acceso per baseline e demo. Se OUT non è più impostato, crea una nuova cartella:

```bash
OUT=$(mktemp -d /home/nvidia/codex-work/newidle-branches.XXXXXX)
```

Imposta TID al worker effettivo. Con demo spenta, altre acquisizioni perf/ftrace
ferme e cyclictest a regime:

```bash
sudo taskset -c 4,5 bash "$K/newidle-events.sh" record "$TID" "$OUT/baseline"
python3 "$K/analyze-newidle.py" "$OUT/baseline.trace"
```

Il comando registra per cinque secondi in un'istanza dedicata: CPU 0, filtro
common_pid=TID, buffer CPU 0 di 16 MiB, nessun function tracer. Salva traccia,
statistiche, stato del worker e contatori kprobe prima/dopo; poi rimuove l'istanza.
Non sovrascrive file esistenti. Non serve leggere trace_pipe durante la cattura.

Avvia CEM come prima, attendi che sia operativo e ripeti senza riavviare cyclictest:

```bash
sudo taskset -c 4,5 bash "$K/newidle-events.sh" record "$TID" "$OUT/demo"
python3 "$K/analyze-newidle.py" "$OUT/demo.trace"
echo "$OUT"
```

## Interpretazione

Il riepilogo usa `paths[motivo, numero_load_balance]=conteggio`.

| Motivo | Decisione osservata |
|---|---|
| 0 | Non classificato: risultato non valido per conclusioni sui motivi |
| 1 | Uscita per wakeup pendente |
| 2 | Uscita perché CPU non attiva |
| 3 | Uscita per tempo idle stimato inferiore a 500 us |
| 4 | Uscita per overload zero, dopo aver superato il controllo idle |
| 5 | Superati i controlli iniziali; ramo di ricerca raggiunto |

Per esempio paths[4,0] conta le uscite per overload zero; paths[5,2] conta le
invocazioni che superano i controlli iniziali ed effettuano due load_balance.
Il riepilogo riporta la quota di invocazioni con motivo 5 e le chiamate medie
per ingresso in quel ramo. Le valutazioni dei domini sono conteggi per dominio,
non per ciclo cyclictest.

- Se baseline esce soprattutto con motivo 4 e demo passa al motivo 5, cambia
  il flag overload effettivamente usato dal kernel.
- Se cambia soprattutto motivo 3, cambia il tempo idle stimato rispetto alla soglia.
- Se la quota di motivo 5 è simile ma crescono le chiamate per invocazione, cambia
  quanto viene percorso il ciclo dei domini.

L'analizzatore rifiuta statistiche con overrun/commit overrun/dropped events
nonzero e invocazioni anomale. Possono esserci eventi di una invocazione già
iniziata all'apertura e una invocazione incompleta alla chiusura: per la prima,
una cattura che contiene eventi interni senza ingresso va ripetuta. Verifica
anche che non crescano i miss-hit nei file profile-before/profile-after per i
nostri dieci eventi: in quel caso ripeti e non usare i conteggi come completi.

Questa prova misura quale decisione cambia. Per identificare quale attività CEM
modifica lo stato interessato e quantificare il contributo alle istruzioni PMU
serviranno acquisizioni successive. La strumentazione stessa aggiunge lavoro.

## Pulizia e recupero

Dopo entrambe le acquisizioni:

```bash
sudo bash "$K/newidle-events.sh" remove
```

Dal Mac, sostituendo il suffisso con quello stampato da echo OUT:

```bash
scp -r jetson-codex:/home/nvidia/codex-work/newidle-branches.XXXXXX ./
```

Validazione dell'assistente: corrispondenza kernel, disassemblato e righe sorgente;
sintassi bash e verifica dell'analizzatore su tracce sintetiche. Preparazione e
registrazione tracefs richiedono sudo e non sono state eseguite dall'assistente.
