# Report grafico

Dalla radice di `jetson-campaign`:

```bash
.venv/bin/python -m pip install -e '.[reports]'
.venv/bin/python analysis/plot_campaign.py results/NOME_CAMPAGNA
```

Output: `report/grafici/report.md` con le tavole SVG impaginate, `index.html`,
`plot-data.json`, `pages.json` e `sources.json`. Il numero di tavole dipende dagli
eventi raccolti: per ogni metrica con `:u` / `:k`, prima il totale e poi la
separazione user/kernel. Vale anche per eventi e nomi di passata personalizzati.

Per ottenere anche il PDF vettoriale, aggiungere `--pdf` allo stesso comando.
Il vecchio comando `export_graph_pdf.py` rimane disponibile e rigenera il report
con il PDF usando i raw verificati della campagna.
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
- PMU: media aritmetica dei conteggi su finestre nominali della durata della campagna.
  Durate diverse nello stesso confronto vengono rifiutate.
  Per eventi separati, il totale di ogni run è `evento:u + evento:k`, nella stessa
  passata e nello stesso scope; poi si calcola la media dei totali. Se manca una
  componente o non è valida, il totale è N/D. Una componente presente e valida
  resta visibile nel grafico separato. Per campagne storiche si usa il contatore
  senza suffisso, quando non sono stati raccolti eventi separati.
  I componenti originali e la passata effettiva restano in `plot-data.json`.
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

Il numero di ripetizioni può essere ridotto e sbilanciato: i grafici non stabiliscono una
significatività statistica né provano che un core sia migliore. I retry e le passate
parziali possono introdurre selezione; l'appendice è esplorativa.

Il confinamento dipende dalla configurazione della campagna (affinità o cpuset).
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
cartella originale della campagna. Le tavole SVG e il PDF opzionale sono vettoriali.
Il generatore dei grafici è locale e indipendente dal runner: non cambia la
provenienza del codice di acquisizione né impedisce la ripresa di campagne avviate.
