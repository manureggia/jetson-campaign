# Report grafico

Dalla radice di `jetson-campaign`:

```bash
.venv/bin/python -m pip install -e '.[reports]'
.venv/bin/python analysis/plot_campaign.py results/NOME_CAMPAGNA
.venv/bin/python analysis/export_graph_pdf.py results/NOME_CAMPAGNA/report/grafici
```

Output: report/grafici/index.html, 18 tavole PNG e SVG, PDF, plot-data.json e sources.json.
Nessuna connessione alla Jetson, nessuna modifica ai raw o all'orchestratore.

## Metodo

- Per ogni ripetizione, nella generazione più recente, si seleziona il primo tentativo
  PASS scaricato e verificato; se non esiste, si conserva l'ultimo tentativo come parziale.
  Tutti i manifest selezionati vengono verificati tramite SHA-256 prima del plotting.
- Il confronto principale usa solo tentativi interamente PASS. I cerchi mostrano le
  ripetizioni; le barre i valori aggregati. n indica CPU 0 / CPU 3.
- Latenze: solo passata core, per non mescolare il protocollo task_perf/profiling.
  Minimo dei minimi, media aritmetica di reported_mean_us (avg del JSON cyclictest),
  massimo dei massimi. La media non è pesata né ricalcolata dall'istogramma.
- PMU: media aritmetica dei conteggi su finestre nominali di 300 secondi.
  Rapporti: somma numeratori / somma denominatori della stessa passata, mai tra
  finestre diverse. I punti rappresentano i rapporti delle singole ripetizioni.
- Task cyclictest e CPU vittima sono scope diversi: il primo comprende avvio e kernel
  nel contesto del task, il secondo tutti i task/IRQ eseguiti sulla CPU monitorata.
- BASELINE non ha processi interferenti: per quello scope si mostra N/D, non zero.
- Refill ratio: refill/accessi, senza dedurre hit rate o un tasso di miss end-to-end.
  L1I comprende L0 macro-op; L2 unified; L3 attribuibile al core e refill con risposta
  esterna al cluster. BUS_ACCESS è beat core-SCU, non byte DRAM. Vedere docs/AUDIT.md.
- Interrupt: delta degli snapshot /proc/interrupts della passata core, media fra run;
  nessuna interpretazione LLM. Le categorie sono quelle del parser esistente.
- Appendice: aggiunge singole passate già validate di tentativi FAIL/INCOMPLETE, con
  rombi e numerosità per pannello. Le passate interrotte, mancanti o con issues vengono
  escluse. Non si uniscono retry diversi per costruire una ripetizione artificiale.

## Limiti da conservare con i grafici

Il numero di ripetizioni DEMO è ridotto e sbilanciato: i grafici non stabiliscono una
significatività statistica né provano che un core sia migliore. I retry e le passate
parziali possono introdurre selezione; l'appendice è esplorativa.

Il protocollo usa affinità ereditata e controlli periodici, non un cpuset esclusivo.
Monitor, servizi, IRQ e attività kernel possono contribuire ai contatori della vittima.
Il precedente falso positivo sospetto con TID 0 rimane da investigare.

Per gli interferenti il runner usa perf -p sui TGID osservati e inherit per nuovi figli.
Il player ciclico ha un processo shell padre e un figlio: possibili effetti dell'aggancio
congiunto e del ricambio dei PID sull'attribuzione dei contatori non sono stati
quantificati. Interpretare questi grafici come conteggi dei processi monitorati dal
protocollo, non come una misura certificata del traffico esclusivo dell'applicazione.

Nel caso della campagna 20260914-163316, il primo avvio non ha acquisito dati ed è
stato seguito da un cambio di boot e dalla ripresa; confrontare i metadata dei tentativi.
Le misure positive non includono il tentativo iniziale interrotto al precheck.

plot-data.json conserva ogni valore, numerosità, formula e lista delle sorgenti;
sources.json conserva gli hash dei manifest e del generatore. I raw restano nella
cartella originale della campagna. Le tavole SVG sono gli originali vettoriali per
esportazione; il PDF contiene le corrispondenti tavole raster a 170 dpi.
