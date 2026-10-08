# Misura PMU diretta del tratto overload → ritorno

L'eseguibile è in `/home/nvidia/codex-work/newidle-pmu-preparation/newidle-pmu`.
Sorgente locale in `tools/newidle-pmu/`. Non usa BTF e non richiede pacchetti
aggiuntivi. La build esatta viene controllata tramite /sys/kernel/notes.
Compilazione GCC con -Wall -Wextra -Werror e test delle istruzioni BPF superati
sulla Jetson. Caricamento nel verifier e lettura PMU reale richiedono sudo:
il solo test del bytecode non li verifica.

## Cosa misura

Due probe: newidle_balance.constprop.0+156 e ritorno della stessa funzione.
Il primo legge il registro ARM64 x0, operando overload già caricato dal kernel,
poi il contatore instructions:k tramite bpf_perf_event_read_value. Il secondo
legge di nuovo il contatore e accumula la differenza. Il contatore dei segmenti è sempre attivo su CPU 0, pinned e senza overflow:
le due letture sono filtrate per il worker e delimitano un tratto con IRQ e
preemption disabilitati, nel quale non avviene un cambio di task. Un secondo
contatore, per il solo worker, misura il totale instructions della finestra.
I due contatori devono risultare senza multiplexing. La prima verifica con
un contatore per-task anche per i segmenti ha mostrato 45 errori di lettura;
non è stata usata per stime. Ora si registrano anche errno e fase degli errori.

Classifica le invocazioni con overload=0 e overload=1; non misura le uscite
prima della verifica overload, né tutto newidle_balance. Il conteggio è
inclusivo del tratto successivo al controllo: comprende bilanciamento,
funzioni discendenti e parte del lavoro dei probe. Il confronto tra le due
classi usa le stesse due sonde e lo stesso codice di lettura.

Il BPF dispone di un interruttore esplicito in una mappa: resta disarmato
durante setup, viene armato solo dopo l'attivazione PMU, poi disarmato prima
che il contatore venga spento. Il ritorno pendente può completarsi mentre
il contatore è ancora attivo. Questo è necessario perché trace_call_bpf
precede il controllo perf dell'evento fermo: PERF_EVENT_IOC_DISABLE sul
probe non basta a disattivare il programma BPF. I due primi check con
45 EBUSY all'ingresso sono invalidi e non vanno usati per le stime.

Gli errori di lettura, nesting, contatore non monotono, miss, ritorni pendenti,
assenza di coppie e multiplexing invalidano il risultato. I probe vengono
rimossi anche su errore o SIGINT/SIGTERM. SIGKILL non consente cleanup; in quel
caso controllare prima che nessuna acquisizione li utilizzi e rimuovere soltanto
ni_pmu/gate e ni_pmu/end. Non avviare più istanze contemporaneamente.

## Verifica iniziale sulla Jetson

Non lasciare altre acquisizioni bpftrace, ftrace o perf attive. cyclictest resta
FIFO 90 su CPU 0, stessi parametri. Se esistono ancora i vecchi eventi,
rimuoverli soltanto dopo aver terminato le acquisizioni che li usano:

```bash
sudo bash /home/nvidia/codex-work/overload-origin-preparation/overload-events.sh remove
sudo bash /home/nvidia/codex-work/newidle-path-preparation/newidle-events.sh remove
K=/home/nvidia/codex-work/newidle-pmu-preparation
TID=31815
OUT=$(mktemp -d /home/nvidia/codex-work/newidle-pmu.XXXXXX)
set -o pipefail
sudo taskset -c 4,5 "$K/newidle-pmu" measure "$TID" 2 \
  2>&1 | tee "$OUT/check.txt"
```

Il JSON deve avere valid=true, errori a zero e almeno una classe con calls>0.
Se il caricamento fallisce, il programma stampa il log del verifier. Conservare
l'output e risolvere prima di passare alla coppia baseline/demo.

## Acquisizione

Conserva K, TID e OUT. Ogni condizione raccoglie tre coppie di 15 secondi:
una finestra count senza probe, una measure con i due probe. Il wrapper salva
stato del worker, task, stdout/stderr e rifiuta file già esistenti.

Con CEM effettivamente ferma:

```bash
sudo taskset -c 4,5 bash "$K/run-newidle-pmu.sh" "$TID" "$OUT" baseline
```

Avvia CEM e il bag come nelle acquisizioni precedenti, aspetta il regime e
mantieni acceso lo stesso cyclictest:

```bash
sudo taskset -c 4,5 bash "$K/run-newidle-pmu.sh" "$TID" "$OUT" demo
printf '%s\n' "$OUT"
```

## Interpretazione e controllo overhead

Per ciascuna classe: media = sum/calls. La differenza tra media ramo lungo e
media uscita overload=0 nella stessa condizione stima il costo aggiuntivo del
tratto lungo, con l'overhead comune delle due sonde in buona parte compensato.
Non è una sottrazione esatta dell'overhead: cache, cammini dei probe e perturbazione
dello scheduler possono differire. Controllare min/max e ripetibilità delle medie.

La modalità count legge soltanto instructions:k per il worker, senza sonde.
Confrontare istruzioni/s count e measure nella stessa condizione: la differenza
è un controllo dell'impatto complessivo della strumentazione in finestre separate,
non un valore esatto da sottrarre a ogni invocazione. Confrontare anche il ritmo
dei cambi di contesto dai file status, tenendo conto dei confini di cattura.

Moltiplicare il costo aggiuntivo stimato per gli ingressi al ramo lungo/s e
confrontarlo con il delta totale demo-baseline nelle finestre count. Questo è
un modello di attribuzione da validare con ripetizioni e controllo overhead;
non assumere anticipatamente che spieghi tutto il delta. Non sommare conteggi
di funzioni annidate come load_balance e newidle_balance.

Riferimento del helper e lettura locale del contatore:
[kernel Linux 5.15](https://github.com/torvalds/linux/blob/v5.15/kernel/trace/bpf_trace.c).
