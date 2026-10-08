# Verificare se CPU 0 scarta i thread CEM per affinità

L'acquisizione risponde a questa domanda: il bilanciamento eseguito sotto il worker cyclictest esamina thread CEM ma li esclude perché non possono essere eseguiti su CPU 0?

Usa le sonde tracefs della build già verificata, senza dipendenze aggiuntive. Non modifica affinità, scheduler o workload. Il filtro dei probe è sul worker e la cattura riguarda CPU 0. Non raccogliere contemporaneamente perf, bpftrace o altre tracce precedenti: la strumentazione aggiunge lavoro e questa prova serve a identificare decisioni, non a misurare il delta PMU senza perturbazione.

## 1. Sulla Jetson, preparare il worker

```bash
K=/home/nvidia/codex-work/load-balance-affinity-preparation
bash "$K/tools/load-balance-affinity.sh" preflight
ps -eLo pid,tid,cls,rtprio,psr,comm,args | grep '[c]yclictest'
```

Alla preparazione non è stato trovato un worker attivo. Se manca ancora, avviarlo in un altro terminale sulla Jetson:

```bash
sudo taskset -c 0 cyclictest -a 0 -t 1 -p 90 --policy=fifo -m -i 1000 -h 1000
```

Lasciare lo stesso cyclictest acceso per entrambe le condizioni. Tornare al terminale di controllo e ricavare il TID del worker FIFO 90 (classe FF), non quello del thread di gestione:

```bash
ps -eLo pid,tid,cls,rtprio,psr,comm,args | grep '[c]yclictest'
TID=12345  # sostituire con il TID appena trovato
OUT=$(mktemp -d /home/nvidia/codex-work/lb-affinity.XXXXXX)
set -o pipefail
sudo bash "$K/tools/load-balance-affinity.sh" setup 2>&1 | tee "$OUT/setup.txt"
```

Setup deve verificare le note del kernel e creare 18 eventi. Se una definizione viene rifiutata, conserva setup.txt: gli eventi già creati in questo tentativo vengono rimossi e non si deve passare alla cattura. Il precedente TID 31815 non è un valore da riutilizzare senza verifica.

Prima della demo, il comando seguente controlla tutti i thread dei tre processi:

```bash
python3 "$K/tools/analyze-load-balance-affinity.py" --check-cem
```

Deve confermare che i tre processi sono presenti e tutti i thread osservati hanno CPU consentite comprese fra 1 e 3. I prefissi di acquisizione che iniziano con demo eseguono automaticamente questo controllo prima di attivare il tracing. Un'affinità 0–5 provoca un arresto senza cattura. Restano salvati gli snapshot prima/dopo per controllare la configurazione ai margini della finestra.

## 2. Baseline e demo

Con stack CEM e player fermati, non soltanto con la riproduzione in pausa:

```bash
sudo taskset -c 4,5 bash "$K/tools/load-balance-affinity.sh" record "$TID" "$OUT/baseline" 3 \
  2>&1 | tee "$OUT/baseline-run.txt"
```

Avviare CEM e il bag con la stessa configurazione delle prove precedenti, mantenendo invariata l'affinità. Aspettare il regime. La maschera di affinità esistente è l'oggetto della verifica, quindi non includere CPU 0 appositamente per questo test.

```bash
sudo taskset -c 4,5 bash "$K/tools/load-balance-affinity.sh" record "$TID" "$OUT/demo" 3 \
  2>&1 | tee "$OUT/demo-run.txt"
sudo bash "$K/tools/load-balance-affinity.sh" remove
printf '%s\n' "$OUT"
```

Ogni acquisizione dura tre secondi e produce traccia, statistiche, miss prima/dopo, stato del worker, snapshot dei task e delle loro affinità, riepilogo testuale e JSON. Non sovrascrive i risultati. L'istanza di tracing viene disabilitata e rimossa anche se l'analisi fallisce. Gli eventi definiti restano fino a remove.

Il risultato deve riportare `VALIDO: True`. Se ci sono errori, conservare l'intera cartella e risolverli prima di interpretare i conteggi; non cancellare una cattura non valida per ripetere sullo stesso prefisso. Usare, per esempio, demo2.

## 3. Come leggere il risultato

La tabella dei thread identifica TGID (processo), TID, nome e numero di candidati osservati, con le categorie:

- AFFINITA: il kernel ha attraversato il ramo che esclude la destinazione in can_migrate_task. È un evento della decisione reale, non una deduzione dalla maschera fotografata.
- RUNNING: il controllo task_running ha escluso il candidato.
- CACHE: il criterio di cache/località ha escluso il candidato; il parser distingue il caso in cui il kernel lo supera dopo tentativi falliti.
- PER_CPU: thread kernel legato a una CPU.
- MIGRATI: candidato accettato, distaccato e inserito su CPU 0 nella stessa chiamata, con ritorno load_balance coerente.
- ALTRI_AMMISSIBILI: ha superato can_migrate_task ma non è stato distaccato. Non viene attribuito all'affinità: restano i successivi criteri di carico in detach_tasks, che questa cattura non distingue fra loro.

`long_cycles` conta i passaggi newidle contenenti load_balance e seguiti da uno switch del worker. `long_to_idle` usa next_pid=0 di sched_switch, non la sola presenza di una funzione chiamata idle_cpu.

`long_idle_all_examined_rejected_affinity` conta i passaggi lunghi che selezionano idle, senza trasferimenti diretti né richieste attive, nei quali c'è almeno un candidato e tutti i candidati esaminati sono esclusi per affinità. Non significa che siano stati scanditi tutti i task della coda.

Gli altri passaggi verso idle sono distinti in ricerche senza candidati e ricerche con motivi diversi o misti. Gli esempi nel JSON riportano le righe originali della traccia, CPU sorgente/destinazione e identificativi del thread. Collegare TGID e TID ai file affinity-before/after per il comando completo: comm può essere troncato.

L'ipotesi riceve riscontro se compaiono esclusioni AFFINITA dei thread CEM verso CPU 0, nello stesso tentativo che termina senza trasferimenti e con switch a idle. Se prevalgono ricerche senza candidati o altre esclusioni, occorre correggere la spiegazione in base ai risultati.

## 4. Scope e controlli

Il contatore moved del ritorno load_balance riguarda i trasferimenti diretti. Il valore di ritorno newidle_balance è registrato separatamente: può indicare lavoro disponibile o necessità di ripetere la selezione, e non è sempre il numero di task migrati.

Le eventuali richieste active_balance sono marcate: il lavoro differito sulla CPU sorgente non viene seguito da questa cattura CPU 0 e non viene chiamato una migrazione fallita. Se risultano frequenti, il seguito è una cattura separata su quelle CPU.

sched_migrate_task è registrato per gli eventi emessi su CPU 0, ma non viene associato al tentativo solo per vicinanza temporale. I trasferimenti diretti sono identificati attraverso distacco e inserimento dentro la stessa chiamata load_balance. sched_switch registra i cambi da/verso il worker su CPU 0.

Il parser rifiuta perdite di buffer, incrementi dei miss, motivi/esiti incoerenti e abbinamenti incompleti interni. Può scartare una invocazione tagliata a ciascun margine della finestra; i conteggi conclusivi usano cicli completi seguiti da switch. Un'acquisizione senza passaggi lunghi può essere valida, ma non risponde da sola all'ipotesi.

mask_lo fotografa i bit 0–63 di cpus_ptr: basta per le sei CPU online di questa Jetson, ma non è una lettura atomica con il controllo kernel. Il ramo osservato è la prova dell'esclusione; la maschera è informazione di contesto.

## 5. Punti di misura verificati

Kernel build ID `40157ac384cbddc13766cb58d82594769fbd09fb`, note SHA256 `e09c7f29fe0eabd63df26ade5734c070fdf58d3b9b3a8ee020b8d9ca33ecdaf4`. Non utilizzare dopo un cambio di kernel. Offset decimali:

| Simbolo | Offset | Punto osservato |
|---|---:|---|
| load_balance | 360 | Risultato find_busiest_group, prima del cbz |
| load_balance | 656 | Coda selezionata x20, prima del cbz |
| load_balance | 676 | Coda sorgente valida |
| load_balance | 904 | Candidato x27 prima del controllo per-CPU |
| load_balance | 916 | Risultato del controllo per-CPU |
| can_migrate_task.part.0 | 64 | Ramo affinità negativa, dopo tbnz non preso |
| can_migrate_task.part.0 | 292 | Operando del controllo task_running |
| can_migrate_task.part.0 | 468 | Confronto fra tentativi falliti e cache_nice_tries |
| load_balance | 1036 | Chiamata set_task_cpu del candidato distaccato |
| load_balance | 2824 | Richiesta stop_one_cpu_nowait per active balance |

Le sonde su ingresso/ritorno newidle, load_balance e can_migrate_task, più attach_task e pick_next_task_idle, completano gli abbinamenti. Registri e layout sono quelli del disassemblato della build, non layout Linux generici.

## 6. Verifiche prima della cattura

```bash
bash -n "$K/tools/load-balance-affinity.sh"
python3 "$K/tests/check_load_balance_affinity.py"
```

Queste verifiche controllano sintassi, parser con sequenze sintetiche, rilevazione dei miss e corrispondenza degli offset col disassemblato. Non verificano la registrazione delle kprobe nel kernel o una cattura reale: questo avviene nei passaggi setup e record, che richiedono sudo.
