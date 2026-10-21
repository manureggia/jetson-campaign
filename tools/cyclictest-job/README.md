# cyclictest-job

cyclictest di rt-tests 2.2, la stessa versione del pacchetto Ubuntu installato sulla Jetson, con una
fase di lavoro sulla memoria eseguita **dopo** ogni campione di latenza. La latenza di risveglio resta
misurata esattamente come nel cyclictest originale. In più, per ogni ciclo, vengono misurati:

- **exec**: dal risveglio (lettura dell'orologio di cyclictest) alla fine del lavoro;
- **response**: dal rilascio programmato alla fine del lavoro (latenza + exec).

Il caso di studio è la piattaforma H3 Vision (Ishii et al., IROS 2009), con due lavori:

- `--job-write=1310720` (profilo `h3_frame`, `campaign-thesis-base-h3.yaml`): scrive un frame 1024 × 1024
  a 10 bit per periodo da 1 ms, come a 1000 fps, in uno slot di un anello da 64 MiB, così i dati sono
  freddi. È equivalente in byte e periodo, non in istruzioni.
- `--job-algo=h3track` (profilo `h3_track`, `campaign-thesis-h3track.yaml`): esegue la parte che in H3 gira
  sul PC, cioè l'algoritmo vero; le istruzioni sono le sue per costruzione.
- le due opzioni insieme (profilo `h3_frame_track`, stessa campagna): prima scrive il frame, poi esegue il
  tracking. È il caso peggiore di un sistema in cui il frame passa dalla RAM della CPU; in H3 il frame
  resta sulla scheda FPGA.

Impronta di `h3track` (contando le linee da 64 B toccate): circa 165 KB di label map letti per attivazione
più circa 1 KB di ingresso; in tutto il ciclo dei 1000 frame la zona toccata della label map è 194 KB su 2 MB,
perché i marker stanno su un disco di circa 280 px. Quindi il lavoro di `h3track` sta quasi tutto nella L2
del core (256 KB). L'impronta da 1,31 MB, sempre fredda, c'è solo con `--job-write`.

## L'algoritmo `h3track` (`h3track.h`)

In H3 l'FPGA fa la parte O(N²) del tracking di marker colorati (estrazione del colore, labeling dei 16
blocchi in parallelo) e passa al PC i momenti m0, mx, my di ogni label di blocco. Il PC fa la parte O(M),
implementata qui come nella sezione IV del paper:

1. **merging (1-c)**: un marker a cavallo tra due blocchi ha una label per blocco; le label vicine al
   confine e più vicine di una soglia vengono unite sommando i momenti;
2. **corrispondenza tra frame (2)**: i numeri di label del frame precedente sono scritti nei loro baricentri
   in una label map 1024 × 1024 (2 MiB); per ogni marker del frame corrente si scandisce una finestra
   quadrata attorno al baricentro e si prende il numero più vicino, altrimenti si crea una label nuova.

I dati in ingresso sono i momenti che l'FPGA darebbe per l'esperimento del paper: 60 marker su un disco a
26 giri/s visto a 1000 fps. 1000 frame sono 26 giri esatti, quindi l'ingresso (circa 1 MB) si ripete senza
salti. Il paper non dà la geometria del disco, quindi è un'ipotesi: 5 anelli da 12 marker con raggi
40–136 px, marker di raggio 4 px, blocchi = 16 strisce orizzontali da 64 righe. Lo spostamento massimo è
22,2 px per frame, quindi la finestra è 51 × 51 px. Le posizioni sono calcolate senza libm e quantizzate a
1/16 px, così l'ingresso è identico su ogni piattaforma; il suo hash FNV-1a finisce nel JSON.

Prima della misura `job_init` esegue due passate su tutto l'ingresso e confronta le label con la verità a
terra (marker che cambia label, due marker con la stessa label, marker non uniti): con un errore cyclictest
si ferma. Poi azzera il tracker, così la misura parte sempre dallo stesso stato.

`h3track_test.c` compila lo stesso header fuori da cyclictest (`cc -O2 -o h3track_test h3track_test.c`) e
stampa hash, errori e tempo per frame. Sul Mac (Apple Silicon, cache calde): hash `60849da8c6219b05`,
0 errori, 67,4 label per frame di cui 7,4 da unire, circa 110 µs per frame. Con una finestra troppo
piccola (21 × 21) il controllo trova 73.480 errori, quindi il check funziona.

## Opzioni aggiunte

| Opzione | Significato |
|---|---|
| `--job-write=BYTES` | byte scritti per attivazione, in ordine lineare, in uno slot freddo |
| `--job-algo=h3track` | parte CPU del tracking H3 per ogni attivazione; con `--job-write`, dopo la scrittura |
| `--job-ring=MIB` | anello di slot di `--job-write`, default 64 MiB: ogni attivazione usa lo slot successivo |
| `--job-hist-ns=W` | larghezza dei bucket degli istogrammi exec/response, default 100 ns, range 10 ms |
| `--job-trace=FILE` | a fine run, per ogni ciclo due `uint32` little-endian: latenza ns, exec ns |

Senza `--job-write`/`--job-algo` il ciclo è quello originale. Con tutte e due, in `job` del JSON `write`
è diverso da 0 e `algo` vale `h3track`. Con il lavoro attivo servono `-N` e `-t 1`.
Tutta la memoria del lavoro (anello o ingresso e label map), gli istogrammi e la trace sono allocati prima
dell'avvio, bloccati in memoria e pre-toccati. Se il lavoro supera il periodo, i rilasci saltati vengono
contati in `job.missed` e non producono campioni di latenza.

Nel JSON (`--json`) il thread ha un oggetto `job` con `write`, `algo`, `ring_bytes`, `slots`, `hist_ns`,
`cycles`, `missed`, ed `exec`/`response` con istogramma (chiave = indice di bucket), `overflow`, `min`,
`max`, `avg` in ns. Con `h3track` c'è anche `h3`: `markers`, `frames`, `window`, `input_hash`,
`check_errors`, `matched`, `created`, `merged`.

## Build sulla Jetson

Copiare in una cartella di lavoro `rt-tests-2.2.tar.xz` (da
`https://mirrors.edge.kernel.org/pub/linux/utils/rt-tests/older/`), `cyclictest-job.patch`, `h3track.h`,
`h3track_test.c` e `build.sh`, poi eseguire `./build.sh`. Lo script controlla lo SHA-256 del tarball,
applica la patch, compila, esegue `h3track_test` e stampa lo SHA-256 del binario `cyclictest-job`.

Binari sulla Jetson:

- `/home/nvidia/codex-work/cyclictest-job-20261014/`, SHA-256
  `9928a217f39cd04ad628dd0cf75e5edfdaf576a1940a02fea779aee59637c739`: la prima versione della patch, con
  `--job-read` e `--job-pattern` oltre a `--job-write`. È quella di `campaign-thesis-base-h3.yaml`, che usa
  solo `--job-write`, uguale nelle due versioni.
- `/home/nvidia/codex-work/cyclictest-job-h3track/`, SHA-256
  `b9966e3e726f17f228d95cb7cb79b8c1f376e29bf7e91662b2febb5a40d2ce68`: `--job-write` e `--job-algo` non
  combinabili. È quella di `campaign-h3track-interference-cpu3.yaml` (già eseguita).
- `/home/nvidia/codex-work/cyclictest-job-h3-20261021/`, SHA-256
  `c594e2464666504685e33ac8f091df999049a9330e2dca702015b8da8c5f10d1`: questa patch, con le due opzioni
  combinabili; è quella di `campaign-thesis-h3track.yaml`.

## Verifiche fatte il 14 ottobre 2026 (CPU3 senza cpuset, 20 s per prova)

- A/B senza lavoro: cyclictest originale 2,635/2,636/2,679 µs, cyclictest-job 2,624/2,664/2,634 µs.

Log, JSON e trace sono in `results/jetson-results/cyclictest-job-20261014/`.

## Verifiche fatte il 21 ottobre 2026 (binario h3track, CPU3 senza cpuset, clock fissi a 1,728 GHz)

`smoke_h3track.sh`, 20 s per prova; istruzioni e cicli utente per attivazione con `perf stat` su run da
10 e 30 s, differenza / 20.000 attivazioni (l'inizializzazione si annulla).

- `h3track_test` sulla Jetson: hash dell'ingresso `60849da8c6219b05` come sul Mac, 0 errori.
- A/B senza lavoro: binario vecchio 3,02/3,29 µs di latenza media, nuovo 3,06/3,43 µs.
- `h3_frame`: exec medio 95,4 µs con entrambi i binari; 655.893 istruzioni per attivazione col nuovo,
  655.916 col vecchio, quindi i due sono equivalenti per `campaign-thesis-base-h3.yaml`.
- `h3track`: **1.286.148 istruzioni** e 429.392 cicli per attivazione (IPC 3,0); exec medio 248,5 µs,
  massimo 267,4 µs; con interfgen su CPU0–2 257,5 µs, massimo 336,5 µs. Nessun rilascio saltato, check
  0 errori, 60 label create e poi sempre ritrovate (1.199.940 corrispondenze su 20.000 frame).

La prima build scriveva il frame rileggendo `job.cycles` a ogni scrittura (984k istruzioni invece di
656k, stessi cicli); corretta con una variabile locale. I risultati di quella build sono in `smoke_v1/`.
Tutto in `results/jetson-results/cyclictest-job-h3track-20261021/`.

## Verifiche fatte il 21 ottobre 2026, binario con `h3_frame_track`

Stesso `smoke_h3track.sh`, esteso al profilo combinato. Risultati in
`results/jetson-results/cyclictest-job-h3-20261021/`.

- `h3track`: 1.286.158 istruzioni per attivazione, come la build precedente; exec medio 248,5 µs.
- `h3_frame_track`: **1.941.562 istruzioni** (la somma di 655.914 della scrittura e 1.286.158 del tracking)
  e 597.630 cicli per attivazione. Exec medio 345,3 µs, massimo 362,1 µs; con interfgen 410,6 µs,
  massimo 609,2 µs. Latenza media 3,66 µs, con interfgen 5,81 µs (`h3track` da solo: 3,16 e 4,37 µs).
  Nessun rilascio saltato, check 0 errori.
