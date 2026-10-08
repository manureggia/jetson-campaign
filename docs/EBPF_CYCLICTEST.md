# Mini guida: lo scheduler viene eseguito nel contesto di cyclictest?

Obiettivo: osservare con eBPF il **thread corrente** all'ingresso nelle funzioni di bilanciamento e lo **stack kernel** che le ha chiamate. Confrontiamo demo spenta e stack CEM operativo, mantenendo lo stesso cyclictest.

> **Stato verificato: il test con kprobe non è eseguibile sul kernel attuale.** Il 29 settembre 2026 la configurazione letta da `/proc/config.gz` sulla Jetson riporta `# CONFIG_KPROBES is not set` e `# CONFIG_DYNAMIC_FTRACE is not set`, pur avendo `CONFIG_BPF=y`, `CONFIG_BPF_SYSCALL=y` e `CONFIG_BPF_EVENTS=y`. Tracefs è già montato sia in `/sys/kernel/tracing` sia in `/sys/kernel/debug/tracing`. Non proseguire con i comandi kprobe dei punti successivi finché non viene avviato un kernel adatto. Il corpo della guida resta una procedura per quel futuro ambiente, non un test validato sul dispositivo attuale.

Il crash di `bpftrace -l 'kprobe:...'` su `available_filter_functions` è coerente con l'assenza di dynamic ftrace. Anche aggirando il listing, la disabilitazione di `CONFIG_KPROBES` impedisce di collegare le sonde richieste. Root, un nuovo mount o un collegamento simbolico non abilitano una funzione esclusa dalla compilazione del kernel.

Per controllare localmente:

```bash
zcat /proc/config.gz | grep -E 'CONFIG_(BPF_SYSCALL|BPF_EVENTS|KPROBES|KPROBE_EVENTS|DYNAMIC_FTRACE)'
```

Il test diretto richiede un kernel Jetson compatibile con `CONFIG_KPROBES=y` e `CONFIG_KPROBE_EVENTS=y`, conservando eBPF/perf; per il listing usato da questa versione di bpftrace va inoltre verificata la disponibilità di `available_filter_functions` tramite dynamic ftrace. La fattibilità di queste opzioni va verificata sui sorgenti e sulla configurazione esatti di NVIDIA/PREEMPT_RT, prima di preparare un altro kernel. Non è una modifica applicabile con un comando sysctl.

Con il kernel attuale resta utilizzabile il tracer ftrace `function`, già usato per le due catture: documenta gli ingressi nelle funzioni sotto il TID cyclictest. Le eventuali sonde eBPF temporizzate o sui tracepoint disponibili sono un'altra capacità e non sostituiscono i conteggi diretti di `newidle_balance`/`load_balance` di questa guida.

## 1. Prerequisiti

La guida usa **bpftrace**. Dopo l'installazione dell'utente, il controllo via `jetson-codex` del 29 settembre 2026 rileva `bpftrace 0.14.0-1` e kernel `5.15.148-rt-tegra`. Non sono stati installati o modificati pacchetti dall'assistente.

Il collegamento che hai creato da `/usr/local/sbin/bpftool` a `/usr/lib/linux-tools-5.15.0-194/bpftool` rende disponibile **bpftool**, ma non installa **bpftrace**. I due strumenti possono coesistere: bpftool ispeziona e gestisce oggetti eBPF, bpftrace compila e collega le sonde descritte qui.

**Errore `Could not resolve symbol: /proc/self/exe:BEGIN_trigger`.** Il pacchetto rilevato ha un eseguibile stripped e `readelf -Ws` non mostra `BEGIN_trigger`/`END_trigger`. Questo è coerente con un [problema noto di packaging di bpftrace](https://github.com/bpftrace/bpftrace/issues/1440). Non dimostra che le sonde kernel eBPF siano inutilizzabili. Lo script qui sotto evita entrambi i blocchi `BEGIN` e `END`.

Come primo controllo, sulla Jetson prova una sonda temporizzata, senza `BEGIN`:

```bash
sudo bpftrace -e 'interval:s:1 { printf("Sonda eBPF interval OK\n"); exit(); }'
```

Deve stampare il messaggio dopo circa un secondo e terminare. Il successo verifica questa sonda temporizzata; le kprobe vanno ancora verificate. Se fallisce, conserva l'errore.

Collegati dal Mac, se necessario, poi controlla le sonde sulla Jetson:

```bash
ssh jetson-codex
bpftrace --version
sudo bpftrace -l 'kprobe:__arm64_sys_clock_nanosleep'
sudo bpftrace -l 'kprobe:newidle_balance*'
sudo bpftrace -l 'kprobe:load_balance'
sudo bpftrace -l 'kprobe:can_migrate_task*'
```

Ogni ricerca deve restituire la funzione richiesta. I suffissi come `.constprop.0` e `.part.0` sono previsti dai wildcard. Se mancano sonde o compaiono errori di permessi/attach, fermati e conserva l'errore: **eBPF abilitato non garantisce che le kprobe necessarie siano utilizzabili**. Questo script non legge strutture kernel e non richiede BTF.

## 2. Individua il worker e prepara la prova

Usa lo stesso assetto precedente: cyclictest su CPU 0, un worker FIFO 90, periodo 1 ms. Se è già attivo, lascialo in esecuzione. Individua il suo worker:

```bash
ps -eLo pid,tid,cls,rtprio,psr,comm | grep '[c]yclictest'
```

Scegli il **TID** della riga con classe `FF`, priorità `90` e CPU `0`, non il PID del thread di gestione. Impostalo qui sostituendo `1234`:

```bash
TID=1234
OUT=$(mktemp -d /home/nvidia/codex-work/ebpf-cyclictest.XXXXXX)
echo "$OUT"
```

Durante queste prove lascia spenta la registrazione ftrace precedente ed evita misure perf simultanee. Mantieni invariati affinità, periodo e configurazione di cyclictest.

## 3. Crea la sonda

Incolla tutto il blocco; gli apici intorno a `EOF` sono necessari:

```bash
cat > "$OUT/context.bt" <<'EOF'
kprobe:__arm64_sys_clock_nanosleep,
kprobe:newidle_balance*,
kprobe:can_migrate_task*
/tid == $1/
{
    @calls[comm, tid, cpu, probe] = count();
}

kprobe:load_balance
/tid == $1/
{
    @calls[comm, tid, cpu, probe] = count();
    if (!@seen) {
        printf("Primo load_balance: comm=%s tid=%d cpu=%d\n", comm, tid, cpu);
        printf("%s\n", kstack(24));
        @seen = 1;
    }
}

interval:s:5 { exit(); }
EOF
```

`tid` e `comm` identificano il task corrente, anche quando esegue codice kernel. I conteggi vengono stampati automaticamente alla fine, senza un blocco `END`; lo stack viene raccolto solo al primo `load_balance`, per limitare il lavoro della sonda. L'eventuale riga finale `@seen: 1` è solo il flag interno che evita di ristampare lo stack: ignorala. Non si stanno contando istruzioni macchina.

## 4. Acquisisci baseline e demo

Con **demo spenta**, nello stesso terminale:

```bash
set -o pipefail
sudo taskset -c 4,5 bpftrace "$OUT/context.bt" "$TID" 2>&1 | tee "$OUT/baseline.txt"
```

Attendi la conclusione automatica. Avvia lo stack CEM (RouDi, segmentazione, pose estimation e player) nel suo terminale abituale; attendi che sia operativo. **Non riavviare cyclictest**. Esegui:

```bash
sudo taskset -c 4,5 bpftrace "$OUT/context.bt" "$TID" 2>&1 | tee "$OUT/demo.txt"
```

`taskset` tiene il processo di controllo bpftrace sui core 4-5; la sonda eBPF viene comunque eseguita sulla CPU che attraversa la funzione osservata. Conserva eventuali errori: una prova con attach fallito o output perso non equivale a un conteggio nullo.

## 5. Come leggere il risultato

Cerca una riga di questo tipo (**formato illustrativo, non risultato misurato**):

```text
Primo load_balance: comm=cyclictest tid=<TID scelto> cpu=0
```

Questa riga prova che, all'ingresso in `load_balance`, il task corrente è proprio il worker cyclictest. Lo stack dovrebbe ricondurre al percorso seguente, letto dal chiamante verso la funzione osservata:

```text
clock_nanosleep -> hrtimer_nanosleep -> do_nanosleep
-> schedule -> ... -> newidle_balance -> load_balance
```

Nell'output reale l'ordine è normalmente inverso; nomi, frame intermedi e suffissi possono variare o mancare per ottimizzazioni/unwinding. Se lo stack è vuoto o troncato, l'identità del task corrente resta osservabile, ma il percorso completo non è verificato. Non interpretare l'assenza di un frame come prova che quella funzione non sia stata eseguita.

Confronta quindi `@calls` nei due file:

- `__arm64_sys_clock_nanosleep`: circa 5.000 chiamate per acquisizione con periodo 1 ms; è il denominatore per confrontare le prove.
- `load_balance / clock_nanosleep`: frequenza del lavoro di bilanciamento per ciclo; ci aspettiamo un aumento nella demo, da verificare.
- `can_migrate_task* / clock_nanosleep`: controlli sui candidati alla migrazione per ciclo. Somma eventuali varianti dello stesso nome.

Una chiave assente vale zero solo se la sonda si è collegata correttamente e il TID è giusto; se manca anche `clock_nanosleep`, verifica prima il worker. Per consolidare il confronto ripeti almeno tre coppie, salvandole con nomi diversi.

**Conclusione consentita:** se nome/TID e stack corrispondono e i conteggi crescono con CEM, hai verificato che il worker attraversa più spesso il percorso kernel di bilanciamento. La sonda non identifica i task candidati, il motivo del ramo lungo o quante istruzioni PMU siano attribuite al worker: sono verifiche successive. Le misure delle istruzioni vanno ripetute separatamente, senza questa strumentazione.

Dal Mac recupera la cartella, sostituendo `XXXXXX` con il suffisso stampato da `echo "$OUT"`:

```bash
scp -r jetson-codex:/home/nvidia/codex-work/ebpf-cyclictest.XXXXXX ./
```

Riferimenti: [bpftrace: kprobe, variabili correnti e stack](https://bpftrace.org/docs/0.21), [scheduler Linux 5.15 upstream](https://github.com/torvalds/linux/blob/v5.15/kernel/sched/fair.c#L10091-L10210).

Validazione della guida: blocchi shell controllati con `bash -n`; versione, simboli dell'eseguibile, mount e configurazione kernel verificati sulla Jetson in sola lettura. Sonde non eseguite dall'assistente. È stato accertato che il kernel attuale non supporta le kprobe richieste; vedere l'avviso iniziale.
