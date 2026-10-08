# Controllo minimo con Telegram

Il bot è un programma separato sul Mac, senza nuove dipendenze e senza porte da aprire.
Mac acceso, collegamento Internet e SSH alla Jetson devono restare disponibili.
Supporta una chat privata abbinata per volta e seleziona la campagna più recente a ogni comando. Messaggi di altri utenti,
gruppi, modifiche ai messaggi e comandi precedenti all'avvio vengono ignorati.

## Prima configurazione

1. In Telegram apri **@BotFather**, verificando che sia l'account ufficiale, e invia
   `/newbot`. Scegli nome e username: BotFather ti consegna il token.
2. Apri la chat privata del nuovo bot e premi **Avvia**.
3. Sul Mac, in un nuovo terminale zsh, entra nella cartella `jetson-campaign` e leggi
   il token senza mostrarlo né scriverlo nella cronologia:

   ```zsh
   read -s "TELEGRAM_BOT_TOKEN?Token del bot: "
   export TELEGRAM_BOT_TOKEN
   ```

4. Avvia il programma; la campagna più recente sotto `results/` viene scelta automaticamente:

   ```bash
   caffeinate -i .venv/bin/python telegram_control.py --pair
   ```

5. Il terminale stampa `/pair CODICE`: invialo nella chat del tuo bot entro cinque
   minuti. L'associazione viene salvata localmente in `.telegram-state.json`, escluso
   da Git. Il token resta solo nell'ambiente del processo e non viene trasferito alla Jetson.

Agli avvii successivi ometti `--pair`. In un nuovo terminale rileggi il token.
Per bloccare eccezionalmente il bot su una campagna specifica resta disponibile
`--campaign results/NOME_CAMPAGNA`; `--results-dir` cambia invece la directory da scandire.
Il programma non avvia una campagna automaticamente e non richiede webhook.
Usa un bot dedicato: un secondo lettore di `getUpdates` interferirebbe con questo programma.

## Comandi dal telefono

| Comando | Effetto |
|---|---|
| `/status` | Scheda leggibile con campagna, barra di avanzamento, test/passata corrente, ETA, heartbeat ed eventuale errore |
| `/logs` | Estratti limitati degli ultimi log della passata |
| `/diagnose` | Parere leggibile del backend LLM su stato, errore e log, senza eseguire azioni |
| `/resume` | Avvia il controller, recupera gli artefatti e riprende anche i tentativi falliti |
| `/restart` | Ferma il controller gestito dal bot, segnala SIGTERM al worker identificato e riprende il tentativo in una nuova directory |
| `/help` | Elenco dei comandi |

Quando `perf_cpus` collide con la CPU vittima o con il placement di DEMO/INTERFGEN, il controller
si ferma prima del pre-hook e pubblica una richiesta persistente. Il bot invia automaticamente una
spiegazione con pulsanti per usare i core liberi, autorizzare la collisione o fermare la campagna.
La callback è accettata soltanto dalla chat privata abbinata e vale per l'intera campagna. Ollama
può migliorare il testo della spiegazione, ma maschere e opzioni sono sempre calcolate dal runner.
Senza risposta entro dieci minuti il controller usa il fallback solo se valido per tutta la matrice;
in caso contrario termina prima di avviare workload.

`/restart` interrompe l'intero tentativo: le sue passate ripartono dall'inizio. Non
riavvia la Jetson e non ripete tentativi già PASS. Il vecchio tentativo resta nei
risultati e viene scaricato prima di proseguire. Boot ID e identità del processo
impediscono di segnalare un PID riciclato. I filtri CPU/scenario originali sono mantenuti.

**Run già avviata in un altro terminale:** puoi leggerne subito stato e log. Per
abilitare il controllo completo, premi una volta Ctrl-C nel terminale del controller
originale (non in quello del bot), poi invia `/resume` al bot. Il worker remoto
continua la propria passata durante il passaggio. Il bot rifiuta `/resume` e `/restart`
finché un controller esterno detiene il lock: non avvia due controller concorrenti.

Se il bot viene chiuso, i processi già avviati continuano. Al successivo avvio usa
`/status`; un controller ancora attivo viene trattato come esterno. Per una campagna
nuova avviala normalmente dal terminale, poi effettua il passaggio sopra descritto.

L'LLM riceve fase, esito, errore ed estratti dei log, restituisce internamente JSON validato e non può impartire comandi.
Con LLM disabilitato o indisponibile `/status`, `/logs`, `/resume` e `/restart`
funzionano comunque. Lo stato del worker non dimostra da solo la correttezza della DEMO.

Avanzamento ed ETA non sono inventati dall'LLM: derivano dal piano, dalle passate concluse
e dalla durata reale dei test PASS già osservati. Prima di avere campioni reali viene usata
la durata nominale. La stima non include retry futuri, pause o indisponibilità della Jetson.

Le risposte e gli estratti richiesti passano attraverso Telegram. Prompt e risposte
LLM, snapshot SSH e comandi ricevuti sono conservati sotto `CAMPAGNA/telegram/`.
Il bot segnala la conclusione del controller che ha avviato; non invia aggiornamenti
periodici. Se Mac o connessione si fermano, il bot non può rispondere né notificare.

Ogni comando ricevuto viene registrato come consumato prima dell'esecuzione, per
non ripetere un restart dopo un crash. Se la risposta si perde, consulta `/status`
prima di ripetere il comando.

API utilizzate: [getUpdates e sendMessage, documentazione Telegram](https://core.telegram.org/bots/api).

## Verifiche dell'implementazione

`python -m unittest tests.test_telegram -v`: undici test locali su autorizzazione,
comandi consentiti, lock del controller, compatibilità del codice, selezione originale,
protezione del token, identità del worker e fallback LLM. Segnali e riavvii sono simulati.
La lettura SSH di `/status` è stata verificata sulla DEMO in corso, senza interromperla;
snapshot e log sono in `results/smoke_orin-20260914-160319-25d8cadf/telegram/`.
L'integrazione con i server Telegram resta da provare dopo la creazione del bot.

Compatibilità Ollama 0.34.0 con `qwen3.5:9b`: lo schema con `reason.maxLength=2000`
causa HTTP 400 (`Failed to initialize samplers: failed to parse grammar`). Il vincolo
non viene quindi inviato nella grammatica, ma la validazione Python continua a rifiutare
motivi oltre 2000 caratteri. Lo schema delle altre proprietà resta attivo.
La richiesta reale corretta ha restituito JSON valido; log in `results/ollama-diagnosis/`.
Questo verifica il protocollo, non l'accuratezza del parere: nella prova il modello ha
erroneamente descritto il warning PM QoS come bloccante. Lo stato deterministico del
worker resta autorevole e `/diagnose` non esegue il suggerimento.
