# Cyclictest e CEM: come abbiamo spiegato l'aumento delle istruzioni kernel

Il punto di partenza era questo: quando CEM era attiva, aumentavano le istruzioni eseguite in kernel e contabilizzate al worker di cyclictest. Volevamo capire da dove arrivasse quel lavoro aggiuntivo. Trovare una funzione chiamata più spesso sarebbe stato soltanto il primo indizio: serviva capire quale scelta dello scheduler cambiasse, cosa la facesse cambiare e quanto costasse in istruzioni.

Abbiamo confrontato cyclictest da solo, la **baseline**, con cyclictest mentre lo stack CEM e il player di `cem_multi.mcap` erano operativi, la **demo**. Il worker era configurato FIFO 90, su CPU 0, con periodo di 1 ms. Nella prima acquisizione il suo TID era 12929; nelle successive era 31815. Abbiamo sempre osservato il worker, distinguendolo dal thread di gestione di cyclictest.

Le misure hanno seguito il problema un passo alla volta: prima il contesto di esecuzione, poi il ramo scelto, poi l'origine del flag che governa quel ramo e infine il costo in istruzioni.

## 1. Prima domanda: perché il bilanciamento dello scheduler compare sotto cyclictest?

La prima acquisizione, salvata in `ebpf-cyclictest.J3oCFu`, serviva a verificare se il bilanciamento fosse davvero eseguito mentre il task corrente era il worker. Abbiamo usato sonde eBPF per contare gli ingressi nelle funzioni e acquisire lo stack al primo ingresso in `load_balance`, filtrando sul TID di cyclictest.

| Chiamate osservate in circa 5 secondi | Baseline | Demo |
|---|---:|---:|
| `clock_nanosleep` | 5.020 | 5.039 |
| `newidle_balance` | 4.963 | 4.649 |
| `load_balance` | 8 | 3.684 |

Il risultato mostrava già una differenza forte: con CEM, `load_balance` veniva raggiunta migliaia di volte, mentre nella baseline soltanto otto. Il numero di nanosleep era simile e gli ingressi in `newidle_balance` erano persino leggermente inferiori nella demo.

**Quindi non aumentava il numero di ingressi in `newidle_balance`: cambiava il lavoro eseguito al suo interno.**

La sonda su `load_balance` riportava `comm=cyclictest`, CPU 0 e il TID del worker. Lo stack passava dalla syscall di nanosleep allo scheduler e poi al bilanciamento:

```text
clock_nanosleep → hrtimer_nanosleep → do_nanosleep
→ schedule → __schedule → balance_fair → load_balance
```

Questo spiegava il primo punto apparentemente strano. Quando cyclictest si blocca per dormire, il kernel deve scegliere il prossimo task. Una parte di questa scelta avviene prima del cambio di contesto, quindi mentre il task corrente è ancora cyclictest. Le istruzioni dello scheduler eseguite in quel tratto possono essere contabilizzate al worker, anche se servono a gestire il sistema e altri task.

Nel singolo stack acquisito non compariva un frame separato di `newidle_balance`; i suoi ingressi erano osservati dalla relativa sonda. Lo stack, da solo, non andava quindi presentato come una ricostruzione completa di ogni funzione attraversata.

Avevamo verificato il contesto e trovato il percorso da indagare. Mancava però la risposta alla domanda centrale: **perché con CEM quel percorso veniva eseguito più spesso?**

## 2. Dentro `newidle_balance`: cambia il ramo oppure aumenta il lavoro per ingresso?

Per rispondere abbiamo recuperato i sorgenti e il `vmlinux` della build effettiva. Il confronto con le note del kernel avviato ha verificato che il binario fosse quello giusto: era necessario prima di mettere sonde su istruzioni interne alla funzione.

Nel sorgente abbiamo individuato un controllo che permette di evitare la ricerca di task da bilanciare. Semplificando, dopo altri controlli iniziali:

```text
Se il tempo idle stimato è sotto 500 µs, esci.
Altrimenti, se rd->overload è zero, esci.
Altrimenti, puoi proseguire nel ramo di bilanciamento.
```

La misura successiva doveva distinguere due possibilità. Con CEM si poteva entrare più spesso nel ramo lungo; oppure, una volta entrati, si potevano visitare più domini e chiamare più volte `load_balance`.

Abbiamo registrato le decisioni interne e abbinato ingresso e ritorno di ogni invocazione, contando le chiamate a `load_balance` contenute al suo interno. Il tentativo con bpftrace non riusciva ad agganciare gli offset con la versione disponibile: abbiamo quindi usato gli eventi kprobe di tracefs. I risultati validi sono nella cartella `newidle-branches.1b6flI`.

| Esito di `newidle_balance`, circa 5 secondi | Baseline | Demo |
|---|---:|---:|
| Invocazioni complete | 4.952 | 4.920 |
| Uscita per tempo idle sotto 500 µs | 13 | 405 |
| Uscita per `overload=0` | 4.938 | 1.188 |
| Superamento dei controlli iniziali | 1 | 3.327 |
| Chiamate totali a `load_balance` | 2 | 6.647 |

Nella baseline quasi tutte le invocazioni uscivano perché `overload` era zero. Nella demo, invece, **3.327 invocazioni su 4.920, il 67,62%, superavano i controlli iniziali**.

Una volta entrate nel ramo lungo, quasi tutte eseguivano due chiamate a `load_balance`: 3.320 invocazioni ne eseguivano due e soltanto 7 una. L'unico ingresso lungo della baseline ne eseguiva due, ma un singolo caso non bastava a descrivere la variabilità della baseline.

La distinzione era ora chiara: **cresceva soprattutto la frequenza di accesso al bilanciamento, non il numero di chiamate per ingresso lungo**. Inoltre il controllo sul tempo idle fermava più invocazioni nella demo: non era questo a spiegare l'aumento delle ricerche.

Il controllo determinante era `rd->overload`. La domanda successiva diventava: **chi rendeva quel flag positivo e perché il worker su CPU 0 lo vedeva cambiare?**

## 3. Da dove arriva `overload`: il ruolo delle altre CPU

Il passaggio importante emerso dal sorgente era che `overload` appartiene al **root domain**, una struttura condivisa fra le CPU interessate. Non è un indicatore relativo soltanto alla coda di CPU 0.

In questa build il flag può essere impostato quando una coda passa da meno di due ad almeno due task runnable. Può anche essere riscritto durante le scansioni del dominio, che valutano le code e le eventuali condizioni di misfit, cioè di mancata corrispondenza fra carico del task e capacità della CPU.

Per capire l'origine del cambiamento abbiamo quindi osservato sia le letture del worker sia gli aggiornamenti prodotti sulle altre CPU. Registrare soltanto gli eventi sotto il TID cyclictest avrebbe nascosto i possibili produttori del flag.

Questa acquisizione ha richiesto anche una correzione del confronto: nella prima baseline erano ancora presenti processi CEM negli snapshot. Hai ripetuto la baseline e abbiamo usato quella nuova, conservata localmente in `overload-origin.ugrjoc-repeat`. La demo è rimasta la stessa.

| Osservazione in circa 5 secondi | Baseline ripetuta | Demo |
|---|---:|---:|
| Letture del worker con `overload=0` | 4.946 | 1.162 |
| Letture del worker con `overload=1` | 0 | 3.080 |
| Quota delle letture con `overload=1` | 0% | 72,61% |
| Scansioni che scrivono `overload=1` | 384 | 15.009 |
| Contributi alle scansioni dovuti a `nr_running > 1` | 435 | 25.575 |
| Contributi misfit osservati | 0 | 0 |

La quota del 72,61% si riferisce alle **letture del flag**, eseguite dopo il controllo idle. Non è la quota di tutti i cicli di cyclictest e non ha lo stesso denominatore del 67,62% della misura precedente.

Nella demo, il **91,91% dei contributi con `nr_running > 1` proveniva dalle CPU 1–3**. Sono osservazioni ripetute delle code durante le scansioni: non rappresentano task distinti o percentuali di utilizzo CPU.

Abbiamo inoltre collegato direttamente 2.096 eventi di impostazione del flag a 1 all'inserimento nelle code di thread dei tre processi CEM: 1.207 per il bag player, 493 per pose estimation e 396 per instance segmentation.

Questi dati spiegavano come CEM potesse influenzare il percorso del worker su CPU 0: **le code delle altre CPU modificavano uno stato condiviso, che CPU 0 leggeva per decidere se avviare il bilanciamento**. Nelle tracce osservate prevaleva la condizione di più task runnable; non emergeva un contributo misfit.

Anche nella baseline si verificavano scritture del flag a 1, ma il worker lo leggeva sempre a zero nella finestra acquisita. Questo ci ha impedito di equiparare il numero di scritture alla durata del flag positivo. Non abbiamo nemmeno associato ogni singola scrittura su una CPU a una specifica lettura su un'altra.

A questo punto avevamo una spiegazione del percorso: CEM aumentava le condizioni di overload nel dominio condiviso e il worker entrava più spesso nel ramo lungo. **Restava da vedere se quel ramo costasse abbastanza da spiegare l'aumento delle istruzioni.**

## 4. I contatori confermano l'aumento; il campionamento non riesce a localizzarlo

Abbiamo tolto le sonde precedenti ed eseguito due tipi di misura separati. Con `perf stat` abbiamo contato le istruzioni e i cicli kernel del worker; con `perf record` abbiamo provato a individuare le funzioni nelle quali venivano eseguite le istruzioni. I dati sono in `newidle-instructions.CLdsm2`.

| Misura in circa 15 secondi | Baseline | Demo | Variazione |
|---|---:|---:|---:|
| Istruzioni kernel | 78.292.110 | 141.148.141 | +80,28% |
| Cicli kernel | 68.514.110 | 110.258.256 | +60,93% |
| Cambi di contesto | 15.002 | 15.002 | Nessuna |

L'aumento era confermato: circa **62,86 milioni di istruzioni kernel aggiuntive**, senza aumento del numero di cambi di contesto nella finestra. Il worker eseguiva più lavoro kernel a parità di quel ritmo.

Il profilo a campioni, però, non mostrava stack di `newidle_balance` o `load_balance`. Molti campioni cadevano in `finish_task_switch` e nello sblocco con ripristino degli interrupt. C'erano inoltre IP iniziali in spazio utente pur avendo selezionato un evento kernel-only.

Non potevamo concludere che il bilanciamento fosse assente: lo avevamo già osservato direttamente. La spiegazione compatibile con il sorgente era che il tratto di bilanciamento si eseguisse con interrupt disabilitati e che l'interrupt PMU venisse servito più tardi, quando lo stack della funzione era già scomparso. Questa è rimasta un'interpretazione dei campioni, non una misura diretta del ritardo.

Perciò non abbiamo usato le percentuali del profilo per distribuire le istruzioni totali fra le funzioni. **Il contatore confermava quanto lavoro fosse aumentato, ma il campionamento non diceva in modo affidabile dove fosse avvenuto.**

È questo limite che ci ha portato all'ultima misura: invece di aspettare un campione PMU, leggere il contatore prima e dopo il tratto interessato.

## 5. La misura diretta: quanto costa passare dal percorso corto a quello lungo?

Abbiamo preparato `newidle-pmu` per leggere il contatore al controllo `overload` e al ritorno di `newidle_balance`. La differenza conta le istruzioni del segmento attraversato, distinguendo `overload=0` e `overload=1`.

Il confine è importante: **misuriamo dal controllo overload al ritorno, non tutta `newidle_balance`**. Le uscite che avvengono prima di quel controllo non entrano nei segmenti. Il conteggio include le funzioni chiamate nel tratto e una parte del lavoro delle sonde.

Il contatore dei segmenti è attivo su CPU 0 e i confini sono filtrati sul worker; nel tratto delimitato non avviene un cambio di task. Un contatore distinto misura il totale kernel del solo worker. Abbiamo raccolto tre ripetizioni per condizione, ciascuna con una finestra senza sonde (`count`) e una con le sonde (`measure`), per 12 acquisizioni complessive.

Prima della raccolta abbiamo risolto gli errori dei check iniziali: quelle prove erano invalide e non sono state usate nei calcoli. Le 12 acquisizioni finali in `newidle-pmu.PIksa3` sono tutte valide, senza errori di lettura, miss o ritorni pendenti e senza multiplexing rilevato.

Le finestre **senza sonde** hanno dato:

| Istruzioni kernel del worker, milioni/s | Ripetizione 1 | Ripetizione 2 | Ripetizione 3 | Aggregato |
|---|---:|---:|---:|---:|
| Baseline | 4,240 | 4,241 | 4,241 | 4,241 |
| Demo | 8,505 | 8,566 | 7,708 | 8,260 |

L'aggregato è il totale delle istruzioni diviso per il totale delle durate effettive. L'aumento è **4,019 milioni di istruzioni/s, pari al +94,76%**. La baseline è stabile; la demo varia fra le ripetizioni. Questo risultato e il precedente +80,28% appartengono a finestre diverse: non sono una stessa misura da combinare.

Le finestre **con le sonde** hanno mostrato che nella baseline il percorso lungo era rarissimo: 6 segmenti su 44.804. Nella demo erano 26.808 su 40.129, il 66,80% dei segmenti osservati, pari a circa **592 ingressi lunghi al secondo**.

Per il costo abbiamo confrontato corto e lungo nella stessa condizione demo, dove entrambe le classi avevano molti casi:

| Segmento misurato nella demo | Istruzioni medie |
|---|---:|
| Percorso corto, `overload=0` | 1.020 |
| Percorso lungo, `overload=1` | 7.785 |
| Differenza lungo − corto | **6.765** |

Il segmento lungo costava circa 7,6 volte quello corto. Ogni volta che veniva eseguito al posto dell'uscita corta, il costo aggiuntivo stimato era quindi di circa 6.765 istruzioni.

Avevamo finalmente i due elementi necessari: **quanto spesso si entrava nel ramo lungo e quanto lavoro aggiungeva ciascun ingresso**.

## 6. Il confronto che collega il percorso alle istruzioni di cyclictest

Abbiamo moltiplicato la differenza di costo per la frequenza degli ingressi lunghi:

```text
Costo aggiuntivo per ingresso × ingressi lunghi al secondo

6.765 istruzioni × 592 ingressi/s ≈ 4,01 milioni di istruzioni/s
```

Usando i valori non arrotondati, la stima è 4.007.365 istruzioni/s. Il delta complessivo senza sonde è 4.018.631 istruzioni/s.

| Confronto finale | Milioni di istruzioni/s aggiuntive |
|---|---:|
| Delta totale del worker, misurato senza sonde | **4,02** |
| Costo aggiuntivo stimato del percorso lungo | **4,01** |

La corrispondenza è molto vicina. Il ramo che avevamo individuato all'inizio non aumentava soltanto come numero di chiamate: **il suo costo e la sua frequenza erano compatibili con gran parte dell'aumento delle istruzioni kernel**.

C'è però un limite che deve accompagnare questo confronto. Le sonde incidono sulla misura: il totale con sonde supera quello senza sonde di circa il 66% nella baseline e il 31% nella demo. Sono finestre separate, quindi queste differenze includono anche la variabilità del carico. Sottrarre il corto dal lungo riduce il costo comune delle sonde, ma non elimina ogni perturbazione. Inoltre usiamo una frequenza misurata con sonde per stimare il lavoro nelle finestre senza sonde.

La vicinanza fra 4,01 e 4,02 milioni/s sostiene quindi il meccanismo, ma **non dimostra un'attribuzione causale esatta del 99,7%**. Anche il confronto fra ripetizioni dello stesso indice varia: il modello produce circa l'88%, il 98% e il 116% del rispettivo delta. Non sono finestre simultanee né un intervallo di confidenza; sono un'indicazione del limite di precisione del confronto.

## 7. La conclusione da spiegare al professore

Il risultato dell'indagine si può raccontare così: quando CEM è attiva, aumentano le condizioni con più task runnable nelle code del dominio condiviso, soprattutto sulle CPU 1–3. Questo rende `overload` più spesso positivo. Il worker cyclictest su CPU 0 legge quello stato e, quando supera anche gli altri controlli, entra più spesso nel ramo di bilanciamento di `newidle_balance`.

Il lavoro viene eseguito prima del cambio di contesto, quando il task corrente è ancora cyclictest. Per questo contribuisce alle istruzioni kernel contabilizzate al worker. Nelle tracce dei rami, una volta entrati, vengono eseguite quasi sempre due chiamate a `load_balance`: il cambiamento principale è nella frequenza di ingresso nel percorso, non nel numero medio di chiamate per ingresso.

La misura PMU diretta aggiunge la verifica quantitativa: circa 6.765 istruzioni aggiuntive per segmento lungo, eseguito circa 592 volte al secondo, producono una stima di circa 4 milioni di istruzioni/s, vicina al delta totale osservato senza sonde.

**Abbiamo quindi una spiegazione del perché, supportata sia dalle decisioni dello scheduler sia dai conteggi delle istruzioni. Il percorso di bilanciamento è un candidato dominante per l'aumento osservato; la sua quota precisa resta limitata dalla perturbazione delle sonde e dalla variabilità fra finestre.**

Questa conclusione riguarda le istruzioni kernel. Le misure non dimostrano migrazioni effettive, né quantificano quanto il percorso contribuisca alla latenza di cyclictest. Per rendere più precisa l'attribuzione servirebbero condizioni baseline/demo alternate con fasi CEM comparabili e un controllo migliore dell'effetto della strumentazione.

---

Per ritrovare i dati, le cartelle seguono lo stesso ordine del racconto:

| Passaggio | Cartella locale |
|---|---|
| Contesto del worker e primi conteggi | [ebpf-cyclictest.J3oCFu](</Users/manu/UNIMORE/Tesi Magistrale/profilazione_orin/jetson-campaign/ebpf-cyclictest.J3oCFu>) |
| Decisioni interne e ramo lungo | [newidle-branches.1b6flI](</Users/manu/UNIMORE/Tesi Magistrale/profilazione_orin/jetson-campaign/newidle-branches.1b6flI/ANALISI.md>) |
| Origine di overload, con baseline ripetuta | [overload-origin.ugrjoc-repeat](</Users/manu/UNIMORE/Tesi Magistrale/profilazione_orin/jetson-campaign/overload-origin.ugrjoc-repeat/ANALISI.md>) |
| Contatori e limiti del campionamento | [newidle-instructions.CLdsm2](</Users/manu/UNIMORE/Tesi Magistrale/profilazione_orin/jetson-campaign/newidle-instructions.CLdsm2/ANALISI.md>) |
| Costo diretto dei segmenti e confronto finale | [newidle-pmu.PIksa3](</Users/manu/UNIMORE/Tesi Magistrale/profilazione_orin/jetson-campaign/newidle-pmu.PIksa3/ANALISI.md>) |
