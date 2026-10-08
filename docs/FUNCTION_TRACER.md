# Ftrace: confrontare cyclictest senza e con la demo

L'aumento delle istruzioni kernel è già stato misurato. Qui vogliamo **cercare
quali chiamate o sequenze di chiamate kernel compaiono in più quando gira la demo**.
Facciamo due tracce dello stesso cyclictest: `baseline` e `demo`, poi una diff.
Non occorre rifare le misure perf.

Usiamo il tracer `function`, disponibile nella configurazione del tuo kernel.
Registra gli ingressi nelle funzioni con il relativo chiamante. Il tracer
`function_graph`, che mostra anche i ritorni, non è abilitato sulla tua Jetson.

Il riferimento è la [documentazione ufficiale ftrace per Linux 5.15](https://www.kernel.org/doc/html/v5.15/trace/ftrace.html),
in particolare le sezioni **The File System**, **The Function Tracer** e
**Dynamic Ftrace**, nella parte dedicata a `set_ftrace_pid`.

## 1. Apri un terminale di controllo sulla Jetson

```bash
ssh jetson-codex
sudo taskset -c 4,5 bash
```

Il primo comando si collega alla Jetson. Il secondo apre una shell root,
necessaria per controllare ftrace, sui core **4–5**. Inserisci la password nel
terminale quando richiesta. Tutti i comandi successivi vanno in questa shell,
tranne l'avvio della demo, che farai in un secondo terminale.

Cyclictest girerà sul core **0**. Controllo e lettura della traccia staranno sui
core **4–5**, ma il costo di registrare una funzione eseguita sul core 0 resta
sul core 0: ftrace scrive nel buffer della CPU che sta eseguendo quella funzione.
[Implementazione ufficiale del ring buffer](https://raw.githubusercontent.com/torvalds/linux/v5.15/kernel/trace/ring_buffer.c).

## 2. Prepara il tracer

Crea una cartella per i risultati e un'istanza ftrace dedicata. Sulla tua Jetson
tracefs è già montato in `/sys/kernel/tracing`.

```bash
OUT=$(mktemp -d /home/nvidia/codex-work/ftrace-cyclictest.XXXXXX)
T=/sys/kernel/tracing/instances/$(basename "$OUT")
mkdir "$T"
echo "$OUT"
```

`OUT` è la cartella in cui salveremo i file; `T` contiene i controlli del tracer.
L'istanza dedicata evita di sovrascrivere il buffer principale di ftrace.

Controlla che nell'elenco compaia `function`:

```bash
cat "$T/available_tracers"
```

Ora seleziona il tracer, lasciando la registrazione spenta:

```bash
echo 0 > "$T/tracing_on"
echo function > "$T/current_tracer"
echo 1 > "$T/tracing_cpumask"
```

- `tracing_on=0`: non scrive ancora eventi.
- `current_tracer=function`: registra le chiamate alle funzioni kernel.
- `tracing_cpumask=1`: osserva solo CPU 0. È una maschera esadecimale,
  **non** il numero di CPU da osservare.

## 3. Imposta un buffer ampio sulla sola CPU 0

```bash
echo 64 > "$T/buffer_size_kb"
echo 1048576 > "$T/per_cpu/cpu0/buffer_size_kb"
cat "$T/buffer_total_size_kb"
```

Il primo comando mantiene piccoli i buffer delle altre CPU. Il secondo assegna
**1 GiB al buffer di CPU 0**; il terzo mostra il totale allocato, in KiB.
Scrivere `1048576` nel file globale `buffer_size_kb` assegnerebbe invece circa
1 GiB **a ciascuna CPU**.

Questo è un punto di partenza ampio per la breve acquisizione seguente, **non un
massimo senza OOM già validato**. La Jetson ha circa 7,44 GiB utilizzabili: il
massimo dipende da quanta RAM richiede anche la demo. Prima di acquisire la coppia,
verifica che la demo funzioni con il buffer allocato e controlla `free -h`.
Poi arrestala per la baseline. Se un comando di allocazione fallisce, fermati.

Al punto 6 controlliamo se si sono persi eventi. Se il buffer basta, puoi già
fare la diff; non serve occupare altra RAM. Se non basta, puoi aumentare **solo
quello di CPU 0** e ripetere entrambe le tracce, oppure accorciare entrambe le
finestre. Mantieni sempre la stessa dimensione nei due casi.

## 4. Avvia cyclictest e seleziona i suoi thread

Parti con la demo spenta e senza un altro cyclictest già attivo.

```bash
taskset -c 0 cyclictest -a 0 -t 1 -p 90 --policy=fifo -m -i 1000 -q \
    > "$OUT/cyclictest.log" 2>&1 &
CT_PID=$!
```

Questo avvia cyclictest sul core 0: un worker, priorità FIFO 90, intervallo di
1.000 microsecondi. `-m` blocca la sua memoria e `-q` evita aggiornamenti continui
sul terminale. `$!` salva il PID del processo appena avviato.
**Lo lasciamo acceso per entrambe le tracce**, così confrontiamo la stessa
esecuzione a regime, senza includere due avvii diversi.

Questo comando usa la sola affinità CPU. Se vuoi riprodurre un esperimento con
cpuset isolato, avvia cyclictest con il tuo consueto isolamento e usa il suo PID:
`taskset` da solo non crea quell'isolamento. In ogni caso usa lo stesso assetto
per baseline e demo.

Visualizza i thread:

```bash
ps -T -p "$CT_PID" -o pid,tid,cls,rtprio,psr,comm
```

Deve comparire il worker con classe `FF`, priorità 90 e CPU 0. Copia il suo
**TID**, non il PID del thread principale. Vogliamo seguire il thread che esegue
il ciclo di misura, senza mescolarlo alle chiamate del thread di gestione.

Nel comando seguente sostituisci `1234` con quel TID:

```bash
TID=1234
echo "$TID" > "$T/set_ftrace_pid"
cat "$T/set_ftrace_pid"
```

L'ultimo comando deve mostrare il numero scelto. Se cyclictest è terminato,
oppure il filtro è vuoto o il file non esiste, **non proseguire**: la selezione
richiesta non è stata applicata. Il TID rimane valido finché lasci in esecuzione
questo cyclictest. Questa traccia riguarda il worker; le eventuali istruzioni
aggiuntive del thread principale non saranno rappresentate.

## 5. Registra la baseline

Con cyclictest acceso e demo spenta:

```bash
echo > "$T/trace"
echo 1 > "$T/tracing_on"
sleep 5
echo 0 > "$T/tracing_on"
```

Questi quattro comandi svuotano la vecchia traccia, iniziano la registrazione,
attendono **5 secondi** e la fermano. È una finestra iniziale breve per avere
file gestibili nella diff.

Salva dati e statistiche del buffer:

```bash
cat "$T/per_cpu/cpu0/stats" > "$OUT/baseline.stats"
cat "$T/trace" > "$OUT/baseline.trace"
```

La lettura avviene a registrazione ferma, dalla shell sui core 4–5.
**Non cambiare `current_tracer` prima di salvare:** il cambio può cancellare i dati.

## 6. Registra il caso demo

Nel secondo terminale avvia la demo come fai normalmente, mantenendo la consueta
affinità e lasciando cyclictest sul core 0. Attendi che la demo sia operativa.
Non riavviare cyclictest e non cambiare il filtro.

Nella shell di controllo ripeti la stessa finestra:

```bash
echo > "$T/trace"
echo 1 > "$T/tracing_on"
sleep 5
echo 0 > "$T/tracing_on"
```

Salva con nomi diversi:

```bash
cat "$T/per_cpu/cpu0/stats" > "$OUT/demo.stats"
cat "$T/trace" > "$OUT/demo.trace"
```

Prima della diff apri le statistiche:

```bash
cat "$OUT/baseline.stats" "$OUT/demo.stats"
```

**`overrun`, `commit overrun` e `dropped events`, quando presenti, devono essere
zero in entrambe le prove.** Un valore nonzero significa che mancano eventi:
non confrontare quelle tracce come se fossero complete. Aumenta il buffer se la
memoria lo consente, oppure riduci la durata, poi ripeti la coppia.

## 7. Fai la diff delle chiamate

Una riga di `function` ha questa forma; nomi e numeri qui sono illustrativi:

```text
cyclictest-1234 [000] .... 100.123456: funzione_B <-funzione_A
```

Significa: **il thread 1234 è entrato in B, chiamata da A, sulla CPU 0**.
Una diff dei file grezzi evidenzierebbe quasi tutto perché i timestamp cambiano.
Creiamo quindi due copie con soltanto `funzione <-chiamante`, mantenendo l'ordine:

```bash
awk '/<-/ {sub(/^.*: /, ""); print}' "$OUT/baseline.trace" > "$OUT/baseline.calls"
awk '/<-/ {sub(/^.*: /, ""); print}' "$OUT/demo.trace" > "$OUT/demo.calls"
```

`awk` seleziona le righe di funzione e rimuove il prefisso con thread, CPU e
orario. I file `.trace` originali restano disponibili per approfondire il contesto.

Ora confronta le sequenze:

```bash
diff -u "$OUT/baseline.calls" "$OUT/demo.calls" | less
```

Le righe con `-` appartengono alla baseline; quelle con `+` al caso demo.
Premi `q` per uscire. `diff` restituisce normalmente codice 1 quando trova differenze.

Per trovare prima le chiamate che cambiano di frequenza, conta ogni coppia
funzione/chiamante:

```bash
LC_ALL=C sort "$OUT/baseline.calls" | uniq -c > "$OUT/baseline.counts"
LC_ALL=C sort "$OUT/demo.calls" | uniq -c > "$OUT/demo.counts"
diff -u "$OUT/baseline.counts" "$OUT/demo.counts" | less
```

Questa seconda diff aiuta a scegliere **dove guardare** nella prima: chiamate
nuove oppure chiamate presenti molte più volte. I conteggi sono sulle finestre
di 5 secondi; da soli non dicono ancora se ogni iterazione percorre più funzioni.

## 8. Cerca il percorso aggiuntivo in una singola iterazione

Cyclictest ripete il proprio ciclo: la diff di due file interi può disallinearsi
anche solo perché le finestre iniziano in punti diversi. Per cercare un percorso
più lungo, confronta **due cicli completi delimitati dallo stesso ingresso kernel**.

Un possibile delimitatore, se presente nelle tue tracce, è `hrtimer_nanosleep`.
Verificalo:

```bash
grep -n '^hrtimer_nanosleep <-' "$OUT/baseline.calls" | head
grep -n '^hrtimer_nanosleep <-' "$OUT/demo.calls" | head
```

Se compare ripetutamente in entrambe, estrai il tratto dal secondo ingresso fino
al terzo, escluso. Così eviti il primo ciclo, che potrebbe essere iniziato prima
della registrazione:

```bash
awk '/^hrtimer_nanosleep <-/ {n++} n==3 {exit} n==2 {print}' \
    "$OUT/baseline.calls" > "$OUT/baseline.cycle"
awk '/^hrtimer_nanosleep <-/ {n++} n==3 {exit} n==2 {print}' \
    "$OUT/demo.calls" > "$OUT/demo.cycle"
diff -u "$OUT/baseline.cycle" "$OUT/demo.cycle"
```

Se quel simbolo non compare almeno tre volte, non usare questa estrazione:
individua nelle tue tracce un ingresso ricorrente che delimiti il ciclo di sonno.
Il nome dipende dal kernel e dal percorso effettivamente usato.

**Questa è la diff più vicina alla tua ipotesi:** a parità di ciclo, nel caso demo
potresti trovare un gruppo di chiamate aggiuntive o una sequenza ripetuta più volte.
Controlla poi qualche altro ciclo per capire se il fenomeno ricorre. Una sola
iterazione scelta a caso può non contenere il percorso che cerchi.

Due precisazioni servono a leggere il risultato:

- Il filtro segue il task corrente: possono comparire anche funzioni di interrupt
  eseguite mentre cyclictest è corrente. Usa il file `.trace`, che conserva le
  informazioni di contesto, per distinguerle; il lavoro eseguito da altri thread
  non è incluso in questa selezione.
- `function` mostra ingressi e chiamanti, non ogni istruzione o i ritorni dalle
  funzioni. Una sequenza aggiuntiva può spiegare l'aumento che hai già misurato;
  una diff uguale non esclude, per esempio, più iterazioni di un ciclo interno
  alla stessa funzione. Non è un call graph completo.

## 9. Ferma la prova

Dopo aver salvato entrambe le tracce:

```bash
echo 0 > "$T/tracing_on"
echo nop > "$T/current_tracer"
kill -INT "$CT_PID"
wait "$CT_PID"
rmdir "$T"
chown -R nvidia:nvidia "$OUT"
echo "$OUT"
```

`nop` disattiva il tracer; `kill -INT` ferma soltanto il cyclictest che hai avviato;
`rmdir` elimina l'istanza e libera i suoi buffer. I risultati rimangono in `OUT`,
leggibili dall'utente `nvidia`. Arresta la demo con il tuo comando abituale.
Se interrompi la procedura prima, esegui comunque questi passi dopo aver salvato
l'eventuale traccia che vuoi conservare.

Per scaricare tutto sul Mac, usa il percorso stampato dall'ultimo comando:

```bash
scp -r jetson-codex:/home/nvidia/codex-work/ftrace-cyclictest.XXXXXX ./
```

Sostituisci `XXXXXX` con il suffisso reale della cartella.

---

La configurazione del kernel è stata letta via SSH. I comandi di acquisizione
richiedono la tua sessione sudo autenticata e non sono stati eseguiti sulla
Jetson durante la preparazione della guida.
