# Audit e protocollo sperimentale

## Codice e risultati preesistenti

La directory superiore contiene `scripts/run_interference_characterization.sh`, i quattro
wrapper CPU0/CPU3, `scripts/analyze_results.py`, `scripts/generate_detailed_report.py`,
`scripts/demo_flamegraph.py`, runner flamegraph e diagnostica timer. Sono presenti campagne
storiche sotto `results/`, analisi cache in `analisi-perf/` e vecchi test membench.

Il runner esistente avviava BASELINE e INTERFGEN, osservava quattro processi DEMO esterni,
richiedeva pause manuali e offriva solo `--resume-demo`. Le otto passate correnti escludevano
esplicitamente cache del core/interferente. Il parser calcolava percentili ma non deviazione
standard interna; i report filtravano tali scope. I vecchi script membench usavano `sudo`,
`sysctl` e `killall`: non sono adatti al nuovo lifecycle e non vengono eseguiti.

La nuova repository riusa il parsing IRQ/percentili e la verifica stack, oltre agli script
FlameGraph upstream. Il controller e il worker sono nuovi: un wrapper dell'intero runner Bash
non avrebbe consentito un journal per passata, ownership affidabile e un profilo DEMO generico.
Il report è adattato alla nuova struttura, senza alterare o rigenerare i report storici.

## Hardware osservato il 14 settembre 2026

- NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super, Tegra234.
- Cortex-A78AE r0p1, CPU0–5 online, kernel 5.15.148-rt-tegra PREEMPT_RT.
- PMU `armv8_cortex_a78`; nessuna PMU DRAM esposta nell'inventario osservato.
- Sysfs L3 condivisa: CPU0–3 e CPU4–5. `cluster_id` non presente; lscpu da solo non basta
  per decidere il posizionamento.
- Perf 5.15.148. FIFO e perf system-wide non accessibili dalla sessione SSH ordinaria.

## Eventi verificati

I nomi sono quelli della PMU `armv8_cortex_a78`. `doctor` confronta l'encoding in sysfs e
prova l'apertura degli eventi prima della campagna. Non usa alias generici come fallback.

| Metrica | Evento | Encoding |
|---|---|---|
| Cicli | cpu_cycles | 0x11 |
| Istruzioni ritirate | inst_retired | 0x08 |
| Accessi dati memoria | mem_access | 0x13 |
| Accessi bus | bus_access | 0x19 |
| Bus cycles | bus_cycles | 0x1d |
| Stall backend / memoria | stall_backend / stall_backend_mem | 0x24 / 0x4005 |
| L1D accessi / refill | l1d_cache / l1d_cache_refill | 0x04 / 0x03 |
| L1I accessi / refill | l1i_cache / l1i_cache_refill | 0x14 / 0x01 |
| L2 accessi / refill | l2d_cache / l2d_cache_refill | 0x16 / 0x17 |
| L3 attribuibile accessi / refill | l3d_cache / l3d_cache_refill | 0x2b / 0x2a |
| L1D read long miss / L1I long miss | l1d_cache_lmiss_rd / l1i_cache_lmiss | 0x39 / 0x4006 |
| L2 / L3 read long miss | l2d_cache_lmiss_rd / l3d_cache_lmiss_rd | 0x4009 / 0x400b |
| Branch prevedibili / mal predetti, speculativi | br_pred / br_mis_pred | 0x12 / 0x10 |
| Branch ritirati / mal predetti ritirati | br_retired / br_mis_pred_retired | 0x21 / 0x22 |

Fonte: [Arm Cortex-A78AE r0p1 TRM, C2.3](https://documentation-service.arm.com/static/5fb7c9cdca04df4095c1d5e4).
Il 28 settembre 2026, sulla Jetson, sysfs esponeva tutti gli otto nuovi eventi
con gli encoding in tabella. `perf stat` li ha aperti singolarmente e in cinque
gruppi da quattro (L1D, L1I, L2, L3, branch), tutti con exit code 0 e
running percentage 100%. Log: `results/pmu-cache-branch-probe-20260928/`.

Nei raw e negli script esaminati non emerge uno scambio L1/L2: le coppie usate erano
`l1d_cache/l1d_cache_refill` e `l2d_cache/l2d_cache_refill`. Non viene applicata una
correzione retroattiva o silenziosa ai dati storici.

Precisazioni metodologiche documentate rispetto alle descrizioni precedenti:

- L1I access comprende anche la cache L0 macro-op: non descriverlo come puro accesso L1I.
- L2 è unified. I refill sono transazioni cacheable originate da L1, con esclusioni
  documentate per prefetch/stash diretti a L2.
- L3 è il traffico attribuibile al core; refill indica risposte con dati provenienti
  dall'esterno del cluster. Non rappresenta il totale di tutti i refill della cache condivisa.
- Gli eventi `*_lmiss*` contano miss a lunga latenza (read per L1D/L2/L3): non
  sostituiscono `*_refill*` come conteggio generale dei refill del livello.
- `br_mis_pred` conta branch speculativi mal predetti; `br_mis_pred_retired`
  conta i branch ritirati mal predetti che causano flush. Per una frazione sui
  branch ritirati usare nella stessa passata anche `br_retired`.
- BUS_ACCESS misura beat sui canali dati tra core e SCU, non byte o transazioni DRAM.
- L'evento 0x1d duplica CPU_CYCLES sul Cortex-A78AE; correggere la descrizione precedente
  “cicli in cui il bus è attivo”, senza modificare i valori raw storici.

## Finestre e attribuzione

Dodici finestre separate per limitare il multiplexing. Contatori del task includono
inizializzazione, thread e kernel eseguito nel contesto monitorato; non equivalgono
al solo ciclo di misura. `victim_cpu` include anche altri task/IRQ sul core.
Il monitor dell'interferente attacca ciascun TGID corrente una sola volta e lascia
l'ereditarietà abilitata per nuovi figli. Gli insiemi CPU interferente/vittima sono disgiunti.

I contatori non sono letture atomicamente simultanee: i monitor core/interferenti precedono
cyclictest di un piccolo intervallo. I registri dei comandi e `measurement.json` conservano
gli istanti delle acquisizioni. I rapporti fra metriche di finestre diverse non vanno
interpretati come una singola catena di miss end-to-end.

Polling ownership/affinità a 1 s e telemetria a 5 s introducono overhead: fanno parte
del protocollo identico nei tre scenari. L'osservazione periodica non prova l'affinità
di ogni thread transitorio fra due campioni. L'affinità viene comunque ereditata dal lancio.
Il flamegraph è un'acquisizione separata e non entra nelle latenze canoniche.
