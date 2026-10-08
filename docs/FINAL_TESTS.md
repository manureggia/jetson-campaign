# Campagna dei test finali

Fonte: `main.pdf` allegato, capitoli 4 e 5, in particolare tabelle 4.2 e 4.3
(pagine stampate 17-18, pagine PDF 19-20). Il PDF è stato letto senza modificare la tesi.
Ripetizioni, durata, warm-up, pause e raggruppamento PMU sono stati aggiornati
su richiesta di Emanuele l'8 ottobre 2026; i valori sotto descrivono i YAML attuali.

La campagna finale esegue prima `campaign-final-affinity.yaml`, poi
`campaign-final-cpuset.yaml`, usando lo stesso profilo `demos/cem_final.yaml`.
Ogni YAML contiene tre blocchi randomizzati con seed **424242** e sei
condizioni CPU/scenario per blocco. La policy IRQ scelta è quella attuale della
Jetson; i file non la modificano. Il cpuset isola soltanto la vittima corrente.

La raccolta comprende la matrice cyclictest concordata. Membench e le
diagnostiche del capitolo 5 restano separati e sono elencati sotto. I file sono
stati verificati offline; nessuna acquisizione è stata avviata sulla Jetson.

## Matrice principale

| Configurazione | Vittima | Scenari | CPU interferenti | CPU frontend perf |
|---|---|---|---|---|
| Affinità | CPU0 | Baseline, INTERFGEN, CEM | 1, 2, 3 | 4, 5 |
| Affinità | CPU3 | Baseline, INTERFGEN, CEM | 0, 1, 2 | 4, 5 |
| Cpuset esclusivo | CPU0 | Baseline, INTERFGEN, CEM | 1, 2, 3 | 4, 5 |
| Cpuset esclusivo | CPU3 | Baseline, INTERFGEN, CEM | 0, 1, 2 | 4, 5 |

La baseline non ha interferenti applicativi. `same_cluster` risolve le maschere
dalla topologia del target: prima della raccolta devono coincidere con la tabella.
`isolcpu: [0, 3]` isola soltanto la vittima del test corrente, non entrambe insieme.
Il campo `perf_cpus` vincola i frontend perf; non garantisce da solo che ogni
processo ausiliario della raccolta esegua su CPU4-CPU5.

| Parametro | Valore |
|---|---|
| Condizioni | 12: 2 CPU x 3 scenari x 2 configurazioni |
| Ripetizioni indipendenti per condizione | 3 |
| Passate per ripetizione | 6: 3 CPU/interferenti e 3 victim task/cgroup |
| Durata di ciascuna passata | 300 s (5 minuti) |
| Warm-up di tutti gli scenari | 5 s a ogni passata, massimo richiesto 10 s |
| Pausa dopo ogni passata | 0 s |
| Cyclictest | 1 worker, FIFO 90, periodo 1 ms, memoria bloccata |
| Istogramma | 10.000 microsecondi, overflow conservati |
| Copertura minima dei contatori | 90% |

Il runner imposta già un worker cyclictest e il memory locking; riavvia e termina
gli interferenti a ogni passata. Il profilo CEM finale mantiene i quattro
componenti, i comandi, gli input, il riavvio del player e il warm-up di 5 s del
profilo esistente. I parametri di misura sono identici nelle due configurazioni;
il YAML affinità conserva anche il suo hook `sudo jetson_clocks --fan`.

Ogni passata usa tutti i **6 contatori programmabili**, più il contatore dedicato
ai cicli; `task_clock` è software e non consuma slot PMU. Il limite è documentato
nel [Cortex-A78AE TRM, §5.6.2, pagina PDF 407](https://documentation-service.arm.com/static/62502fb164e3265f7c90b120#page=407).
I 18 eventi programmabili distinti per scope richiedono almeno 3 passate;
le passate CPU/interferenti e victim task/cgroup restano separate.

| Passate | Sei eventi programmabili |
|---|---|
| `core`, `victim_core` | Istruzioni user/kernel, backend stall, memory stall, accessi memoria e bus |
| `cache_l1`, `victim_cache_l1` | Accessi, refill e long miss L1D e L1I |
| `cache_l2_l3`, `victim_cache_l2_l3` | Accessi, refill e long miss L2 e L3 |

Tutte conservano `cycles`; `core` e `victim_core` conservano anche `task_clock`.
Nessun evento dei YAML precedenti è stato rimosso o spostato tra scope.
Le latenze principali provengono da `core`; gli altri istogrammi restano separati.
I contatori degli interferenti vengono raccolti nelle passate CPU quando i
processi sono presenti. Profiling e contatori branch aggiuntivi non sono stati
inseriti nella matrice principale.

Totale: **36 ripetizioni, 216 passate, 18 ore di acquisizione**. I warm-up
aggiungono 18 minuti: **18 ore e 18 minuti nominali**, oltre a startup, readiness CEM, trasferimenti,
elaborazione, preflight e retry. Ciascun YAML rappresenta metà della matrice.

## Protocollo concordato e limiti dell'infrastruttura

1. **Randomizzazione e sequenza (§4.3.2).** Ogni YAML produce ora tre blocchi
   randomizzati da sei condizioni; `run` identifica il blocco. L'ordine delle
   passate resta fisso e l'ordine delle misure viene salvato in `order.json`,
   conservato al resume. Il seed è **424242**. Il lancio multiplo completa prima
   il YAML affinità, poi il YAML cpuset. Questo segue la richiesta di campagne
   consecutive. Nel §4.3.2 della tesi sono invece mescolate tutte le 12 condizioni
   nello stesso blocco: i risultati di questa sequenza vanno descritti come due
   campagne consecutive con sei condizioni per blocco.
   La tesi contiene ancora `[randomization seed]` e non è stata modificata.
2. **Policy IRQ e stato cpuset (§4.3.1).** La policy IRQ deve essere identica nelle
   due configurazioni, ma la tesi non specifica le maschere. Il runner salva i
   conteggi `/proc/interrupts` e `/proc/softirqs`, non una verifica della policy
   `smp_affinity_list` per ogni passata. L'helper verifica la partizione durante
   create/exec; mancano un controllo esplicito di partizioni residue nei test di
   affinità e snapshot completi dello stato della partizione prima di ogni passata.
   Emanuele ha scelto di mantenere la **policy IRQ attuale**, da registrare e
   verificare identica in tutte le condizioni; nessun hook che cambi gli IRQ è
   stato aggiunto.
3. **Posizionamento dei collettori (§4.1.1, §5.1.2).** I nuovi YAML collocano perf
   su CPU4-CPU5. Worker, controlli ausiliari e launcher non sono tutti vincolati da
   questo campo. Occorre verificare e completare il posizionamento se la frase
   della tesi riguarda l'intera infrastruttura di raccolta.
4. **Analisi per blocchi e overflow (§4.5).** Il report attuale non produce le
   differenze appaiate rispetto alla baseline nei blocchi, né tutte le
   normalizzazioni descritte. Con overflow usa la media riassuntiva di cyclictest
   e rende indisponibili tutti i quantili; la tesi richiede momenti indisponibili
   e quantili ancora calcolabili quando il rango rientra nei bin disponibili.
   I raw sono conservati: l'analisi va adeguata prima di presentare i risultati.
5. **Prerequisiti hardware (§4.1.2).** Kernel, versioni dei tool, MAXN_SUPER,
   frequenza di 1,728 GHz, topologia, risorse CEM e disponibilità simultanea degli
   eventi devono essere verificati sul target. Questi YAML non cambiano power
   mode, frequenze o configurazione di sistema. La verifica locale non certifica
   il comportamento hardware.

## Test descritti nella tesi e non inclusi: da decidere insieme

| Test | Protocollo presente nella tesi | Cosa manca per includerlo |
|---|---|---|
| Membench (§4.2.2) | CPU0, accesso sequenziale/randomizzato, 0-3 meminterf su CPU1-CPU3, working set crescenti | Lista dei working set, parametri del benchmark, ripetizioni e decisione su quali misure storiche riutilizzare. Il runner cyclictest non esegue direttamente membench. |
| Profiling CPU (§5.3.1) | Perf CPU-wide sulla vittima, cpu-clock a 99 Hz, clock mono, DWARF 8192 | Condizioni, durate e ripetizioni delle finestre diagnostiche. Il supporto esiste; nei nuovi YAML è disabilitato in attesa della scelta. |
| Sampling del worker (§5.3.1) | Instructions:k, periodo 100.000, solo TID worker | Condizioni e durate; raccolta distinta dal profiling CPU e dalle passate principali. |
| Function tracing / eBPF (§5.3.2) | Solo worker e CPU vittima; diagnosi eBPF da 5 s; perdite e contesto | Decidere quali acquisizioni ripetere. Strumenti e risultati precedenti sono separati dalla campagna YAML. |
| Decisioni newidle e stato overload (§5.4.1-5.4.2) | Branch trace da 5 s su CPU0/worker; writer osservati su tutte le CPU online | Confermare le condizioni da ripetere e durata del trace writer; verificare kernel e offset. |
| Costo diretto newidle (§5.4.3) | Baseline/CEM su CPU0, 3 coppie count/measure da 15 s; ordine invertito nella seconda coppia, stesso worker | Decidere se ripetere le diagnostiche; collector in `tools/newidle-pmu/`, distinto dalla matrice principale. |

Questa lista segnala ciò che non è incluso nei nuovi YAML; non afferma che le
acquisizioni storiche siano assenti o inutilizzabili. Non sono stati aggiunti
altri scenari, prove di throughput CEM o stime di bandwidth DRAM.

## Pianifica e avvia

Dalla root del repository, con il virtualenv `.venv` già preparato:

```bash
bash tools/run-final-tests.sh plan
```

`plan` è offline: mostra l'ordine randomizzato di ciascun YAML e le 18 ore di
acquisizione complessive. Eseguendo lo script senza argomenti si ottiene lo
stesso piano. Per avviare la sequenza, con i prerequisiti della Jetson preparati:

```bash
bash tools/run-final-tests.sh run
```

Il controller esegue il preflight di ogni campagna prima di acquisire. La seconda
campagna parte solo dopo il completamento riuscito della prima.
Una failure, una misura incompleta o Ctrl-C ferma la sequenza. La lista non è
una coda persistente: dopo un'interruzione usare `resume --campaign` per la
cartella corrente e poi avviare il YAML cpuset se non è ancora iniziato:

```bash
python -m jetson_tests resume --campaign results/final_tests/NOME_STAMPATO_DAL_CONTROLLER
python -m jetson_tests run --config campaign-final-cpuset.yaml
```

Se è già iniziata la campagna cpuset, basta riprendere la sua cartella. Ogni
campagna ha un report proprio sotto `results/final_tests/`; nomi già occupati
ricevono un suffisso numerico e non vengono sovrascritti. Per un controllo
separato si può usare `doctor` al posto di `run`: dopo `doctor` usare `resume`
sulla cartella stampata per raccogliere nello stesso report.

Verifica offline dell'8 ottobre 2026 con `.venv/bin/python`: entrambi i piani
passano il validatore del progetto e contengono 18 ripetizioni / 108 passate /
9 ore di acquisizione ciascuno, in tre blocchi da sei condizioni. Verificati anche
la conservazione di tutti gli eventi nei rispettivi scope, i sei slot programmabili
per passata e i selettori PMU user/kernel; superati 16 test locali sulla
configurazione PMU e sulla sequenza delle campagne. Questa verifica non avvia workload e
non verifica la disponibilità simultanea dei nuovi gruppi sulla Jetson; il
`doctor` attuale prova gli eventi singolarmente.
