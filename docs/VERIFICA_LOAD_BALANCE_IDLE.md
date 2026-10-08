# Riscontro: bilanciamento seguito dalla scelta di idle

Sono state rilette le tracce function di circa 5 secondi in `/Users/manu/Downloads/ftrace-cyclictest/ftrace-cyclictest.Qmj8oT/`, relative al worker cyclictest TID 8095 su CPU 0. Si tratta dell’acquisizione ftrace precedente alle successive misure dei rami e PMU, non della stessa finestra.

Gli eventi sono stati suddivisi da ogni ingresso in `schedule` al successivo. Sono stati selezionati i segmenti contenenti `newidle_balance.constprop.0` e, dopo tale ingresso, cercati `load_balance` e `pick_next_task_idle`. Ogni segmento selezionato contiene un solo ingresso newidle. Gli stats di entrambe le catture riportano overrun, commit overrun e dropped events zero.

| Sequenza osservata | Baseline | Demo |
|---|---:|---:|
| Segmenti newidle senza load_balance | 4.958 | 2.777 |
| Fra questi, con successivo pick_next_task_idle | 4.958 | 2.777 |
| Segmenti newidle con load_balance | 5 | 1.544 |
| Fra questi, con successivo pick_next_task_idle | 5 | 1.543 |
| Segmenti lunghi senza successivo pick_next_task_idle | 0 | 1 |

Nella demo, 1.543/1.544 = 99,94% dei segmenti lunghi sono seguiti dalla selezione idle. Non è un conteggio di migrazioni fallite e non riguarda tutte le future esecuzioni: è l’esito osservato in questa traccia.

Esempio demo: righe 115 e 132 load_balance, 123 e 125 can_migrate_task.part.0, 154 pick_next_task_idle. Il worker ha richiesto il sonno; il kernel cerca lavoro per la CPU prima del cambio di contesto e in questo caso raggiunge la selezione del task idle. CPU 0 seleziona idle; cyclictest resta bloccato in attesa del timer. La registrazione filtrata sul worker non mostra ciò che gli altri task eseguono durante la sua sospensione.

L’unica eccezione demo è nel segmento che inizia con schedule alla riga 347055: load_balance alla riga 347123, dequeue_task_fair alla 347137, set_task_cpu alla 347149, attach_task alla 347176 ed enqueue_task_fair alla 347178; segue __pick_next_task_fair alla 347215 e pick_next_entity alla 347216, senza pick_next_task_idle. La sequenza supporta uno spostamento FAIR seguito dalla selezione FAIR. La traccia non registra argomenti o task selezionato: non identifica quale thread sia stato spostato o eseguito.

Un ritorno zero di load_balance non significa automaticamente errore: può non esserci squilibrio utile, non esserci una coda sorgente selezionabile, oppure esserci una ricerca con candidati esclusi. Per distinguere questi casi occorrono nuovi eventi.

## Acquisizione proposta

Su una build verificata contro vmlinux, sonde tracefs mirate sotto il worker per: ingresso/ritorno load_balance, selezione gruppo/coda sorgente, candidato in can_migrate_task e rami effettivi di esclusione, controlli successivi in detach_tasks, distacco e inserimento del task. Registrare CPU sorgente/destinazione, TID/TGID/comm e vincolo di affinità rispetto a CPU 0. Le funzioni ottimizzate/inlined richiedono punti dal disassemblato della build attuale.

Aggiungere sched_switch su CPU 0 per conoscere il task effettivamente selezionato, e sched_migrate_task per riscontrare gli spostamenti. Se viene richiesto active balancing, seguire anche il lavoro differito sulla CPU sorgente: non limitarlo al TID cyclictest. Nessuna migrazione può essere attribuita al nostro tentativo solo perché temporalmente vicina.

Il riepilogo deve distinguere: nessuna sorgente utile; candidati esaminati; esclusione per affinità, task in esecuzione, località/cache, thread per-CPU; altri scarti dopo can_migrate_task; task spostati; task scelto su CPU 0. Can_migrate_task=1 non prova lo spostamento; sched_migrate_task da solo non mostra gli scarti.

Catture brevi baseline/demo, senza contemporanea misura PMU; verificare perdite, miss e abbinamenti. L’obiettivo è spiegare i tentativi, non stimare le istruzioni senza perturbazione. Non è stata eseguita una nuova acquisizione né modificato il tracing della Jetson.
