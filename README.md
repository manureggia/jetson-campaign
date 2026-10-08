# Jetson campaign

Orchestratore da macOS, Linux o Windows con WSL per NVIDIA Jetson: campagne riprendibili, DEMO definita in YAML,
misure deterministiche e Ollama opzionale. Questa cartella è una repository Git autonoma.
Gli script e i risultati nella directory superiore non vengono modificati.

## Installazione e primo controllo

Per preparare la Jetson dopo un flash, seguire la [guida Startup Orin](docs/STARTUP_ORIN.md)
(SSH, tool di misura, permessi perf, realtime e cpuset).

Richiede Python 3.10 o successivo sul controller, Git, SSH/rsync e l'alias `jetson-codex` già configurato.
Su Windows eseguire installazione e controller dentro Ubuntu su WSL: il CLI usa `fcntl`,
non disponibile nel Python nativo di Windows. Tenere il checkout e il virtualenv nel filesystem Linux di WSL.
Sulla Jetson il worker usa soltanto la libreria standard Python; non installa pacchetti.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

python -m jetson_tests plan --config campaign/campaign.yaml
python -m jetson_tests doctor --config campaign/campaign.yaml
```

Usare l'installazione **editable da questo checkout**: la distribuzione del worker include
gli script FlameGraph versionati e le impronte dei file del checkout.

`plan` è offline e non avvia workload. Mostra tutte le acquisizioni e la loro durata minima;
setup, warmup, cooldown, elaborazione e retry si aggiungono a questa durata.
`doctor` risolve la topologia reale, controlla i prerequisiti e prova perf su comandi brevi.
Conserva log e diagnostica nella directory locale stampata a terminale.

Il profilo predefinito prevede tre ripetizioni: 18 ore di acquisizioni PMU e 30 minuti
di profiling, più tempi accessori. Per la prima prova modificare una copia della configurazione:
`victim_cores: [0]`, `repetitions: 1`, `duration_s: 10`, solo BASELINE abilitata,
`profiling.enabled: false`.

## Come inserire i comandi della DEMO

La campagna indica il file da usare:

```yaml
scenarios:
  demo:
    enabled: true
    placement: system
    profile: demos/cem.yaml
```

Nel profilo scrivere i comandi **come nel terminale**, uno per processo:

```yaml
name: mia_demo
warmup_s: 20
startup_timeout_s: 120
shutdown_timeout_s: 10

processes:
  - name: server
    command: /opt/mia-app/server --config "config con spazi.yaml"
    ready_log_regex: 'Server ready'

  - name: client
    command: /opt/mia-app/client --input dati.bin
```

I processi partono nell'ordine del file e poi restano attivi contemporaneamente.
Il cleanup li termina in ordine inverso, insieme ai discendenti appartenenti alla campagna.
I comandi devono rimanere in foreground: evitare `nohup`, `setsid`, daemonizzazione e servizi
esterni. Una pipeline o un figlio in background è gestibile se rimane nella sessione del comando.
Il runner esegue ogni comando con `bash -c` in una sessione dedicata.

Campi opzionali del profilo:

```yaml
environment_script: /opt/mia-app/setup.sh
resources:
  dati.bin: /home/nvidia/dataset/dati.bin
  config con spazi.yaml: /home/nvidia/config.yaml
```

`environment_script` prepara l'ambiente di ciascun comando. Le risorse vengono copiate dalla
Jetson alla directory temporanea della campagna: la chiave è il percorso relativo usato dai comandi,
il valore è il percorso assoluto della risorsa originale. Non servono copie dei modelli sul Mac.
Gli input vengono identificati con SHA-256; file nuovi o modificati dal workload vengono recuperati
fra i risultati. Gli input copiati e rimasti invariati restano nella workspace remota e sono
rappresentati localmente da configurazione e manifest. Non eliminare gli originali necessari
alla riproduzione.

Ogni processo può aggiungere:

```yaml
repeat_on_success: true        # ripete dopo exit 0, utile per il player
ready_log_regex: 'READY'       # attende un messaggio nello stdout
# oppure, in alternativa alla regex:
# ready_command: /opt/mia-app/check-ready
```

Il controllo predefinito richiede che il processo rimanga vivo cinque secondi;
segue il warmup del profilo (default 30 s). È un controllo operativo, non dimostra che
l'applicazione stia producendo risultati corretti: impostare una verifica di readiness adatta.
Tutti i processi elencati sono obbligatori. Il riavvio del player a fine file è atteso soltanto
con `repeat_on_success: true`; un exit nonzero è un fallimento deterministico.

Per sostituire la DEMO basta cambiare `demos/cem.yaml`, oppure creare un altro profilo e cambiare
`profile:`. Sono ammessi nomi e numeri di processi arbitrari. Il profilo CEM iniziale deriva
dai comandi documentati nei dati storici: non è un requisito del runner.
Per wrapper shell che nascondono il vero eseguibile, aggiungere `foreign_process_names` nel
profilo (lista dei nomi degli eseguibili reali) per riconoscere istanze estranee già attive.

## Scenari, CPU e misure

`victim_cores: [0, 3]` genera BASELINE, INTERFGEN e DEMO per ciascuna CPU.
BASELINE richiede assenza degli interferenti conosciuti, ma non arresta servizi del sistema.
INTERFGEN avvia un'istanza del comando configurato per ciascuna CPU selezionata.
La DEMO eredita l'intero insieme selezionato; ogni thread osservato deve restare dentro tale insieme.

| Placement | CPU degli interferenti |
|---|---|
| `same_cluster` | CPU online che condividono il cluster/L3 della vittima, esclusa la vittima |
| `system` | Tutte le CPU online escluse la vittima |
| `explicit` | Lista `cpus: [1, 2]`, validata senza rimuovere silenziosamente valori errati |

Confrontare cluster e sistema intero con campagne distinte. La topologia e gli insiemi
effettivi vengono salvati e mostrati prima del lancio.

### Isolamento cpuset, perf e hook globali

`taskset` resta sempre il meccanismo di affinità: fissa cyclictest alla vittima e colloca
INTERFGEN/DEMO sulle CPU risolte da `same_cluster`, `system` o `explicit`. `isolcpu` aggiunge
un cpuset cgroup v2 esclusivo soltanto per la CPU vittima corrente; deve essere un sottoinsieme
di `victim_cores` e accetta uno scalare o una lista:

```yaml
victim_cores: [0, 3]
isolcpu: [0, 3]       # oppure: isolcpu: 0
perf_cpus: [4, 5]     # frontend perf sui core housekeeping

command-pre-test:
  - sudo -n jetson_clocks --store "$JETSON_CAMPAIGN_ROOT/jetson-clocks.before"
  - sudo -n jetson_clocks --fan
command-post-test:
  - sudo -n jetson_clocks --restore "$JETSON_CAMPAIGN_ROOT/jetson-clocks.before"
```

Dentro il cpuset cyclictest conserva anche `taskset -c CPU`. I processi frontend di perf restano
fuori dal cpuset, ma continuano a misurare la CPU o il task vittima; quando `perf_cpus` è presente
sono fissati a quella maschera. Se la maschera collide con DEMO/INTERFGEN, il controller pubblica
una richiesta al bot Telegram. Dopo 10 minuti senza risposta usa core liberi, se disponibili per
tutta la matrice, altrimenti non avvia il test.

Il pre-hook viene eseguito una volta prima del primo test dell'intera matrice YAML; il post-hook
solo a matrice completa o durante una chiusura anomala senza worker attivi. Di conseguenza una run
filtrata riuscita lascia intenzionalmente attivo il pre-hook. I comandi girano come `nvidia` nella
root remota della campagna; un comando privilegiato deve dichiarare esplicitamente `sudo -n`.

L'isolamento richiede l'helper versionato `tools/jetson-campaign-cgroup`, installato manualmente
come `/usr/local/sbin/jetson-campaign-cgroup`, root-owned e non scrivibile da gruppo/altri, più
una regola sudoers NOPASSWD limitata a quell'eseguibile. `doctor` verifica helper, proprietà,
autorizzazione e supporto alle partizioni cpuset prima della campagna. Il helper usa `isolated`
quando supportato e ripiega su una partizione esclusiva `root` sui kernel 5.15; poiché ogni
partizione contiene una sola CPU vittima, non rimane bilanciamento interno. Prima di tornare
all'utente `nvidia`, il helper imposta soltanto per il processo del test il limite `RLIMIT_RTPRIO`
richiesto dalla priorità YAML; il comando non viene eseguito come root. Il cpuset non sposta
automaticamente IRQ vincolati o ogni sorgente di rumore kernel.

Installazione amministrativa una tantum sulla Jetson:

```bash
sudo install -o root -g root -m 0755 tools/jetson-campaign-cgroup /usr/local/sbin/jetson-campaign-cgroup
echo 'nvidia ALL=(root) NOPASSWD: /usr/local/sbin/jetson-campaign-cgroup *' \
  | sudo tee /etc/sudoers.d/jetson-campaign-cgroup
sudo chmod 0440 /etc/sudoers.d/jetson-campaign-cgroup
sudo visudo -cf /etc/sudoers.d/jetson-campaign-cgroup
```

Autorizzare separatamente e in modo altrettanto ristretto gli eventuali programmi privilegiati
scritti nei pre/post hook, per esempio il percorso reale di `jetson_clocks`.

Per default ogni ripetizione misura dodici finestre: `core`, `victim_core`, `memory`, `victim_memory`,
`l1d`, `l1i`, `l2`, `l3` e le quattro corrispondenti `victim_*`.
Gli scope sono `victim_cpu`, `victim_task` e `interferer`. La passata `core` fornisce le latenze
principali; le altre metriche mantengono la propria finestra. I monitor partono prima di cyclictest:
non sono dichiarati perfettamente simultanei. Comandi e timestamp documentano la differenza.
Senza `isolcpu` e senza `perf_cpus`, nelle passate task il processo perf resta vincolato alla
vittima come nel protocollo originale. Con `isolcpu`, perf misura il cgroup della vittima
dall'esterno, evitando la perdita dei contatori quando il figlio attraversa `sudo`.
Configurando `perf_cpus`, il frontend perf viene spostato sui core housekeeping mentre
cyclictest resta sulla vittima.

Un monitor perf che termina con errore interrompe subito l'acquisizione; exit code e stderr
sono salvati in `monitor_failures.jsonl`. Se l'aggancio ai processi interferenti fallisce con
`ESRCH` (task scomparso), la passata riparte con nuovi monitor e cyclictest, fino a tre avvii.
I tentativi sono conservati in `pass_*/acquisition_001/`, `acquisition_002/`, ecc.; soltanto
i file dell'acquisizione valida sono pubblicati direttamente in `pass_*` e usati dal report.
Gli altri errori e l'esaurimento dei tre avvii fanno fallire il tentativo della run.

I flamegraph hanno acquisizioni dedicate a 99 Hz, stack DWARF, ristrette alla CPU vittima.
`perf.data`, output decodificato, stack collapsed e `core.svg` vengono conservati.

I parser rifiutano risultati incompatibili, contatori non contati o con running percentage
inferiore a `perf_min_running_pct` (default 90). Eventi non esposti dall'hardware hanno valore
`null` e motivo esplicito. Un overflow dell'istogramma conserva i dati disponibili ma rende
incompleta la misura: deviazione standard e quantili completi non vengono inventati.
Gli IRQ mancanti, rinominati o decrescenti rendono i relativi totali N/A e producono
avvisi non bloccanti: le passate successive proseguono e latenza/PMU restano disponibili.
A fine campagna il terminale segnala il numero di avvisi; `report/summary.md`,
`report/warnings.csv` e `report/warnings.json` li elencano con passata e metriche di origine.
La decisione di escludere una misura resta a chi analizza i risultati.

Vedere [audit e mappatura PMU](docs/AUDIT.md) per il significato preciso dei contatori.

### Selezionare contatori e passate

Vedere [example.yaml](campaign/example.yaml) per tutte le opzioni, i valori predefiniti,
le alternative e il catalogo completo dei contatori, commentati.
Il prefisso delle passate è `victim_`: aggiornare eventuali YAML con il vecchio
`task_`. I nomi degli scope/raw (`victim_task`, `perf_victim_task.csv`) restano stabili.
La vittima attuale è cyclictest: `cyclictest.command` richiede un eseguibile
compatibile con i suoi argomenti e output, non un comando arbitrario.

`perf_passes` sostituisce le dodici passate predefinite. Ogni voce della mappa è una
finestra di `duration_s` secondi, nell'ordine YAML; la lista contiene gli eventi da
misurare contemporaneamente. I nomi che iniziano con `victim_` misurano cyclictest
(o il suo cgroup con `isolcpu`); gli altri misurano CPU vittima e interferenti.

```yaml
perf_passes:
  victim_instructions: [instructions:u, instructions:k]
profiling:
  enabled: false
```

Questo esempio esegue **una sola passata**, con istruzioni user e kernel separate.
`:u` e `:k` sono i [modificatori di perf](https://man7.org/linux/man-pages/man1/perf-list.1.html).
Il conteggio kernel riguarda l'esecuzione mentre il task/cgroup misurato è attivo,
non tutto il lavoro kernel sulla CPU né una classificazione delle sole syscall.
Il caso isolato usa il filtro [perf stat -G](https://man7.org/linux/man-pages/man1/perf-stat.1.html).
Le finestre task e cgroup conservano le differenze di avvio descritte sopra.

Gli eventi ammessi sono `cycles`, `instructions`, `backend_stall`, `memory_stall`,
`memory_accesses`, `bus_accesses`, `bus_cycles`, `l1d_accesses`, `l1d_refills`,
`l1d_long_miss_reads`, `l1i_accesses`, `l1i_refills`, `l1i_long_misses`,
`l2_accesses`, `l2_refills`, `l2_long_miss_reads`, `l3_accesses`, `l3_refills`,
`l3_long_miss_reads`, `branch_predictions`, `branch_mispredictions`,
`branches_retired` e `branch_mispredictions_retired`
(anche con suffisso `:u` o `:k`) e `task_clock` senza suffisso. Le quattro metriche
`*_long_miss*` sono incluse nelle passate cache predefinite L1D, L1I, L2 e L3,
sia per la CPU sia per la vittima. Misurano miss **a lunga latenza**, distinti dai
`*_refills` già presenti; non rappresentano ogni miss del livello. I quattro eventi
branch sono selezionabili in `perf_passes`, ma non aggiungono passate predefinite.
Per esempio `victim_branches: [branches_retired, branch_mispredictions_retired]`
misura in una finestra i branch ritirati e quelli predetti erroneamente.
La mappatura PMU
continua a essere verificata in sysfs; `doctor` prova i selettori richiesti con i loro
modificatori. Nomi di passata non validi ed eventi duplicati/non validi sono rifiutati.
Non viene calcolato automaticamente un numero di passate in base ai contatori hardware:
se il multiplexing porta il running percentage sotto soglia, dividere gli eventi in
più voci della mappa. Per esempio, aggiungere `victim_cycles: [cycles:u, cycles:k]`
crea una seconda passata. Il profiling resta indipendente e va disabilitato esplicitamente.

Il report prende le latenze da `core` se presente, altrimenti dalla prima passata
configurata. I contatori conservano nome della passata e modificatore nelle colonne CSV.
Gli script storici in `analysis/` assumono le dodici passate: per selezioni personalizzate
usare `report/runs.csv`, `report/runs.json` e `report/provenance.csv`.

Per il confronto rapido sono pronti due file: `campaign/campaign-instructions-base.yaml`
esegue INTERFGEN BASE e DEMO BASE; `campaign/campaign-instructions-isolcpu0.yaml` esegue solo
DEMO con core 0 isolato. Due campagne servono perché `isolcpu` si applica a tutti gli
scenari di una campagna. Entrambe usano core vittima 0, frontend perf su core 4,
placement `same_cluster`, profilo `cem_fast`, una ripetizione da 30 secondi e nessun
profiling: **tre finestre, 90 secondi di acquisizione** più setup, warmup, cooldown e retry.
L'isolamento richiede l'helper già installato e autorizzato descritto sopra.

```bash
python -m jetson_tests plan --config campaign/campaign-instructions-base.yaml
python -m jetson_tests plan --config campaign/campaign-instructions-isolcpu0.yaml
python -m jetson_tests run --config campaign/campaign-instructions-base.yaml && \
python -m jetson_tests run --config campaign/campaign-instructions-isolcpu0.yaml
```

## Esecuzione, resume e risultati

Durante `run` e `resume` la CLI mostra un pannello aggiornato a ogni polling:
test corrente e totale, CPU/scenario, ripetizione e tentativo, numero e nome della
passata, fase del worker e tempo della finestra di acquisizione. La barra indica
l'avanzamento delle acquisizioni; il 100% richiede la conclusione dei test.
Sono visibili anche il tempo trascorso dall'avvio del controller, il tempo residuo
e l'orario di fine stimati per la campagna selezionata. L'ETA iniziale comprende
durata, warmup e cooldown configurati e si adatta ai tempi dei test completati;
setup/elaborazione sono stimati solo dopo aver osservato quei tempi, mentre i retry
futuri restano esclusi. Con `resume`, filtri e nuove generazioni i contatori seguono
i test selezionati. Il pannello si aggiorna sul posto nel terminale; con output
rediretto emette testo semplice ai cambi di fase e almeno ogni 30 secondi durante
il polling, senza sequenze ANSI.

La [campagna finale](docs/FINAL_TESTS.md) contiene i YAML affinità e cpuset con
CPU0/CPU3, Baseline/INTERFGEN/CEM, cinque ripetizioni e seed 424242.
`bash tools/run-final-tests.sh plan` mostra il piano offline;
`bash tools/run-final-tests.sh run` esegue le due campagne in sequenza.

Per randomizzare le condizioni all'interno di ogni ripetizione, aggiungere al YAML:

```yaml
randomization:
  enabled: true
  seed: 424242
```

Ogni blocco contiene una misura per ciascuna coppia CPU/scenario abilitata; il numero
`run` identifica il blocco. Le passate PMU mantengono l'ordine YAML e il profiling,
se abilitato, viene raccolto dopo tutti i blocchi di misura. Senza `randomization`
o con `enabled: false` resta l'ordine originale. Il seed deve essere un intero
non negativo, esplicito quando la randomizzazione è abilitata. L'ordine completo
viene salvato in `order.json`: filtri e `resume` mantengono quell'ordine, saltando
le misure già completate.

Per eseguire più campagne una dopo l'altra, passarle nello stesso comando:

```bash
python -m jetson_tests plan --config campaign-final-affinity.yaml campaign-final-cpuset.yaml
python -m jetson_tests run --config campaign-final-affinity.yaml campaign-final-cpuset.yaml
```

`--config` può anche essere ripetuto. Tutti i YAML e i filtri vengono validati prima
di creare la prima campagna; ogni campagna conserva cartella, preflight, hook e report
propri. La successiva parte solo dopo il completamento riuscito della precedente.
Errori, acquisizioni incomplete o Ctrl-C fermano la sequenza. `doctor` accetta la
stessa lista e controlla le campagne in ordine, senza eseguire la raccolta.
La randomizzazione resta interna a ciascun YAML: campagne consecutive non vengono
mescolate nello stesso blocco.

La lista non crea una coda persistente. Dopo un'interruzione, riprendere la cartella
della campagna corrente con `resume --campaign`, poi lanciare i YAML successivi.
`resume` accetta al massimo un YAML di confronto; `--campaign` indica una sola
cartella ed è utilizzabile con un singolo YAML.

Una nuova campagna usa il nome YAML (`name`) come cartella sul Mac e sulla Jetson:
`results/mia_campagna` e `/home/nvidia/codex-work/mia_campagna`. Se il nome è già occupato,
il controller prova `mia_campagna_1`, `mia_campagna_2` e così via. Non riusa né sovrascrive
cartelle esistenti. Ogni invocazione di `run` o `doctor` crea una nuova campagna;
dopo `doctor`, usare il percorso stampato con `resume --campaign` per acquisire nello stesso
report.

```bash
python -m jetson_tests run --config campaign/campaign.yaml
python -m jetson_tests run --config campaign/campaign.yaml --core 0 --scenario baseline
python -m jetson_tests resume --campaign results/NOME_CAMPAGNA
python -m jetson_tests resume --campaign results/NOME_CAMPAGNA --rerun-failed
python -m jetson_tests resume --campaign results/NOME_CAMPAGNA --force --core 0
python -m jetson_tests report --campaign results/NOME_CAMPAGNA
```

`run --resume --campaign ...` equivale a `resume`. La configurazione risolta salvata è usata
per default al resume; passando anche `--config` viene confrontata integralmente. Per verificare
una modifica al YAML originale, passarlo esplicitamente. Codice, profilo o risorse incompatibili
richiedono una nuova campagna; non si mescolano protocolli differenti.

I tentativi completati vengono saltati. `--rerun-failed` concede una nuova serie limitata di
tentativi falliti; `--force` apre una nuova generazione senza sovrascrivere i precedenti.
Il report seleziona il primo PASS della generazione più recente, non il risultato migliore.
Con `max_retries: 2` ogni generazione ha al massimo tre tentativi.

Il worker gira separatamente dalla sessione SSH. La perdita di connessione non elimina i timeout.
Ctrl-C interrompe il controller sul Mac; il tentativo remoto termina autonomamente entro il proprio
limite. `resume` ne recupera lo stato e i risultati. Un worker interrotto viene riconciliato usando
boot ID e identità dei processi; un errore di cleanup richiede attenzione prima di continuare.
Una sola campagna alla volta può acquisire sulla Jetson (lock sotto `/home/nvidia/codex-work`).

I risultati sono organizzati così:

```text
results/CAMPAGNA/
  sources/                  # YAML originali
  config.json               # configurazione risolta
  provenance.json           # commit e hash dei file del codice
  doctor/                   # probe, stdout, stderr, inventario hardware
  transport/                # registro di SSH e trasferimenti
  llm/                      # richieste, risposte e decisioni
  results/core0/demo/measurement_001/attempt_001/
    metadata.json
    status.json
    transitions.jsonl
    commands.jsonl
    resources.json
    affinity.jsonl
    pass_core/              # cyclictest, perf, snapshot IRQ, metriche e telemetria
    ...
    manifest.json
  report/
    runs.csv
    runs.json
    aggregates.csv
    aggregates.json
    provenance.csv
    attempts.json
    summary.md
    latency.svg
```

La gerarchia `results/core/scenario/measurement/run/attempt/pass_*` conserva le acquisizioni
separate: 12 directory `pass_*` per ogni misura completa, più `pass_profiling` se abilitato.
Retry e `--force` aggiungono tentativi senza sovrascrivere i raw. Sulla Jetson si aggiungono
`code/` (copia del runner) e `workspaces/` (input e file temporanei dei workload); sul Mac
restano solo i raw scaricati e verificati. Tutte queste directory contribuiscono allo stesso
`report/` della campagna.

Ogni tentativo deve essere scaricato e verificato tramite SHA-256 prima di essere consegnato.
Il report separa statistiche tra run e distribuzione pooled dei campioni; le medie pooled
sono pesate per numero di campioni. Con un solo run la variabilità tra ripetizioni è N/A.
Profiling, tentativi falliti e risultati mancanti sono esclusi dagli aggregati principali.

## Ollama e test

Per rigenerare il report con grafici CPU 0/CPU 3, PNG, SVG e PDF:
[istruzioni e metodo](analysis/README.md). Il generatore opera sui raw locali verificati
e separa i tentativi PASS dall'appendice con dati parziali.

Per controllare una campagna dal telefono: [bot Telegram opzionale](docs/TELEGRAM.md).
Il bot gira sul Mac e usa il controller esistente; non cambia il protocollo delle misure.

Impostare `llm.enabled: true` per usare Ollama già disponibile sul Mac.
Il runner non installa Ollama e non scarica modelli.
Default: classificazione della causa probabile nei log dopo un fallimento deterministico.
Per abilitare anche l'interpretazione di progresso ambiguo:

```yaml
features: [error_classification, ambiguous_progress]
```

Le verifiche semantiche di progresso avvengono al massimo una volta al minuto durante RUNNING,
con estratti limitati dei log. Non sostituiscono timeout, exit code, contatori o healthcheck
deterministici. Il modello non riceve strumenti di esecuzione e non modifica i comandi YAML.

`LLMBackend`, `NoLLMBackend` e `OllamaBackend` espongono `analyze(LLMRequest) -> LLMResult`.
Nuovi provider potranno implementare la stessa interfaccia. Il risultato contiene classificazione,
azione, confidence e motivo; JSON, enum e confidence sono validati localmente.
L'orchestratore ammette soltanto `continue`, `retry`, `restart_workload`, `restart_monitor`,
`abort_run`, `request_human_review`, restringendoli allo stato corrente.
Un recovery durante la misura interrompe e conserva il tentativo; la nuova acquisizione riparte
dall'inizio. La confidence non è una probabilità calibrata di correttezza.

Sotto soglia o con Ollama indisponibile prosegue la politica deterministica. Solo le funzionalità
elencate in `required_features` possono richiedere intervento per un'interpretazione non disponibile.
La classificazione post-fallimento annota i log; non modifica un exit code né convalida dati mancanti.

Per valutare il modello sui casi storici etichettati (warning benigno, perf, crash,
affinità, lock e permessi realtime):

```bash
python -m jetson_tests.llm_eval --config campaign/campaign.yaml
```

La suite riporta accuratezza grezza ed errori che superano `confidence_threshold`.
È operativamente valida quando nessuna risposta errata supera la soglia: le risposte
incerte vengono ignorate e resta autorevole la politica deterministica.

```bash
python -m unittest discover -v
```

Le prove sui processi Linux sono saltate sul Mac e vanno eseguite sulla Jetson in una workspace
dedicata, con `TMPDIR` dentro quella workspace. Vedere [validazione](docs/VALIDATION.md).
Il runner invoca `sudo -n` soltanto per l'helper cpuset quando `isolcpu` è attivo; eventuali altri
usi di sudo devono essere scritti esplicitamente negli hook YAML. Non installa pacchetti, non cambia
il kernel e non riavvia il sistema.
