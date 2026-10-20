# cyclictest-job

cyclictest di rt-tests 2.2, la stessa versione del pacchetto Ubuntu installato sulla Jetson, con una
fase di lavoro sulla memoria eseguita **dopo** ogni campione di latenza. La latenza di risveglio resta
misurata esattamente come nel cyclictest originale. In più, per ogni ciclo, vengono misurati:

- **exec**: dal risveglio (lettura dell'orologio di cyclictest) alla fine del lavoro;
- **response**: dal rilascio programmato alla fine del lavoro (latenza + exec).

Le impronte vengono dal modello Amalthea del WATERS Industrial Challenge 2019 (Bosch e UNIMORE):
per esempio il Planner legge 1,28 MB per attivazione e il Lidar Grabber legge 1,5 MB e ne scrive 2.

## Opzioni aggiunte

| Opzione | Significato |
|---|---|
| `--job-read=BYTES` | byte letti per attivazione (tutte le parole da 8 B di ogni linea da 64 B) |
| `--job-write=BYTES` | byte scritti per attivazione, in ordine lineare |
| `--job-pattern=seq\|chase` | lettura lineare, oppure catena di puntatori in ordine casuale (seed fisso) |
| `--job-ring=MIB` | anello di slot, default 64 MiB: ogni attivazione usa lo slot successivo, così i dati sono freddi |
| `--job-hist-ns=W` | larghezza dei bucket degli istogrammi exec/response, default 100 ns, range 10 ms |
| `--job-trace=FILE` | a fine run, per ogni ciclo due `uint32` little-endian: latenza ns, exec ns |

Senza `--job-read`/`--job-write` il ciclo è quello originale. Con il lavoro attivo servono `-N` e `-t 1`.
Anello, istogrammi e trace sono allocati prima dell'avvio, bloccati in memoria e pre-toccati.
Se il lavoro supera il periodo, i rilasci saltati vengono contati in `job.missed` e non producono
campioni di latenza.

Nel JSON (`--json`) il thread ha un oggetto `job` con `read`, `write`, `pattern`, `ring_bytes`, `slots`,
`hist_ns`, `cycles`, `missed`, ed `exec`/`response` con istogramma (chiave = indice di bucket), `overflow`,
`min`, `max`, `avg` in ns.

## Build sulla Jetson

Copiare in una cartella di lavoro `rt-tests-2.2.tar.xz` (da
`https://mirrors.edge.kernel.org/pub/linux/utils/rt-tests/older/`), `cyclictest-job.patch` e `build.sh`,
poi eseguire `./build.sh`. Lo script controlla lo SHA-256 del tarball, applica la patch, compila e stampa
lo SHA-256 del binario `cyclictest-job`. Il binario usato dal pilota è in
`/home/nvidia/codex-work/cyclictest-job-20261014/` e ha SHA-256
`9928a217f39cd04ad628dd0cf75e5edfdaf576a1940a02fea779aee59637c739`, verificato dal pre-test di
`campaign-job-pilot.yaml`.

## Verifiche fatte il 14 ottobre 2026 (CPU3 senza cpuset, 20 s per prova)

- A/B senza lavoro: cyclictest originale 2,635/2,636/2,679 µs, cyclictest-job 2,624/2,664/2,634 µs.
- Lettura sequenziale: Planner 104 µs di exec, Lidar 281 µs; con interfgen su CPU0–2, 248 e 530 µs.
- Catena di puntatori: 4,1–6,7 ms per attivazione. Non sta in un periodo da 1 ms, quindi quei profili
  girano a 10 ms con un riferimento senza lavoro allo stesso periodo.

Log, JSON e trace sono in `results/jetson-results/cyclictest-job-20261014/`.
