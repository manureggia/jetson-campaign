# Validazione

## Come ripetere i controlli

Sul Mac, dall'ambiente della repository:

```bash
python -m unittest discover -v
python -m jetson_tests plan --config campaign.yaml
python -m jetson_tests doctor --config campaign.yaml
```

I test Linux devono essere eseguiti soltanto in una directory dedicata sotto
`/home/nvidia/codex-work/`, impostando `TMPDIR` e `JETSON_TEST_ROOT` a una sua sottocartella.
Trasferire soltanto package, test, configurazioni e strumenti necessari; conservare stdout e
stderr localmente. Non trasferire `.venv` o file privati.

```text
python3 -m unittest tests.test_processes tests.test_worker_linux -v
```

## Copertura

Verifica finale: 33 test complessivi. Sul Mac passano 22 test e vengono saltati gli
11 specifici per Linux; sulla Jetson passano tutti questi 11 test. Il worker reale
è stato inoltre lanciato due volte con la stessa richiesta: una sola esecuzione,
esito INCOMPLETE al precheck atteso e risultati scaricati con SHA-256 verificato.

- Fixture raw storiche cyclictest/perf, precisione interi, istogrammi con overflow,
  delta IRQ con reset/sorgenti scomparse/header CPU cambiato e mappatura PMU errata.
- Matrice, placement cluster/sistema/esplicito, profili sostituibili, campi errati e percorsi
  delle risorse, parametri cyclictest protetti.
- State machine, resume senza duplicati, disconnessione SSH simulata, `--force`,
  configurazione incompatibile, download verificato e rilevamento di raw modificati.
- Statistiche pesate, report da fixture storica, provenienza e validità XML dello SVG.
- Backend disabilitato senza rete, JSON Ollama via server HTTP locale simulato,
  indisponibilità, risposta malformata/ostile, timeout complessivo, confidence bassa,
  budget esaurito, risposta obsoleta e journal idempotente.
- Sulla Jetson: reali processi Linux con ambiente e virgolette, affinità, readiness,
  player ripetuto, timeout/crash, cleanup dei discendenti e dei figli orfani,
  conservazione di un processo estraneo, worker distaccato/idempotenza, recupero dopo
  interruzione e simulazione del cambio boot ID senza reboot.

## Limiti della validazione hardware

La prima sessione SSH aveva `RLIMIT_RTPRIO=0` e `perf_event_paranoid=2`:
il preflight rifiutava FIFO priorità 90, perf system-wide e perf record.
Il 14 settembre 2026, dopo la configurazione dei permessi da parte dell'utente,
una nuova sessione riporta rispettivamente 90 e 0. Il preflight completo passa
su CPU 0 e 3. L'agente non ha invocato sudo né modificato la configurazione di sistema.

Le prove Linux di lifecycle non richiedono FIFO o PMU. Il test del worker dimostra il
fallimento esplicito al precheck e la consegna di artefatti INCOMPLETE; non rappresenta
una misura cyclictest riuscita. I test del controller usano un trasporto simulato.
Il report di test deriva da raw storici chiaramente identificati.

La successiva prova BASELINE su CPU 0 passa sul commit `54c3660`: una ripetizione
con tutte le dodici passate da 10 secondi e un'acquisizione di profiling separata
da 10 secondi. Entrambi i tentativi sono PASS; raw, report e manifest sono stati
scaricati e verificati tramite SHA-256. Il profiling contiene 996 campioni, di cui
988 con stack, senza record non interpretati. Il flamegraph è generato, ma include
8000 frame non risolti, osservati anche negli stack kernel: questa prova verifica
la catena di acquisizione, non la qualità completa della simbolizzazione.

Restano da validare la matrice completa, le durate sperimentali e il profilo CEM
end-to-end: la presenza degli eseguibili e delle risorse non dimostra che inferenza,
input e disponibilità siano corretti durante un run.

Ollama reale non risponde su `localhost:11434` nell'ambiente osservato. Il protocollo HTTP
e la politica di fallback sono verificati con un server locale simulato; non è stata
valutata l'accuratezza semantica del modello `qwen3:8b`.

## Artefatti locali

I risultati voluminosi sono esclusi da Git; codice, configurazioni, test, fixture minime
e questa documentazione sono versionati. I log delle verifiche sono sotto `results/`:

- `local-tests.stdout.txt` e `local-tests.stderr.txt`: suite locale.
- `plan.json`: matrice prodotta dalla CLI.
- `jetson-results/implementation-doctor-2/`: inventario e probe hardware reali.
- `jetson-results/lifecycle-validation/`: prime undici verifiche Linux complete.
- `jetson-results/release-validation/`: verifica finale sullo snapshot di codice
  incluso nel primo commit; `provenance.json` ne conserva gli hash. Il commit non
  esisteva ancora al lancio, quindi il relativo campo è `null`.
- `jetson-results/permissions-doctor/`: preflight completo dopo i nuovi permessi.
- `jetson-results/baseline-smoke/`: acquisizione reale breve PMU e profiling,
  raw, flamegraph, manifest verificati e report. Configurazione originale della
  prova anche in `results/baseline-smoke.yaml`.

Ogni directory di validazione conserva `campaign.json`, che identifica la relativa workspace
remota, e `transport/`, con comandi SSH, exit code, stdout, stderr e durate.
