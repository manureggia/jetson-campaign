# Startup della Jetson Orin dopo un flash

Questa guida ripristina l'ambiente di misura per `jetson-campaign`: SSH, tool,
accesso a perf senza sudo, priorità realtime, isolamento cpuset e strumenti di
tracing. I programmi, i modelli, le risorse e gli script di ambiente della demo
sono considerati già presenti: per questi usa la guida dedicata.

**Dove eseguire i comandi:** sulla **Jetson come utente `nvidia`**, salvo i blocchi
marcati **Mac**. I comandi amministrativi di installazione richiedono inizialmente
la password di sudo; le acquisizioni successive usano i permessi persistenti.
Se l'utente è diverso da `nvidia`, adatta i percorsi e le regole che lo nominano.

Il progetto installa il comando **`jetson-tests`** e il modulo **`jetson_tests`**.
Non definisce un comando `jetson_test`: se lo usavi, era un alias o un wrapper
esterno al repository. Il controller gira sul Mac e trasferisce automaticamente
il worker sulla Jetson; sulla Orin non serve installare il pacchetto Python del
controller né copiare il virtualenv del Mac.

> Stato della guida: basata sul codice del runner e sulla documentazione dei tool.
> Il 1 ottobre 2026 il controllo SSH del dispositivo ha restituito
> `Permission denied (publickey,password)`. Il nuovo kernel e questi comandi non
> sono quindi stati verificati sulla Jetson riflashata. Le vecchie guide di tracing
> descrivono il kernel precedente: ricontrolla le capacità al punto 8.

## 1. Identifica il sistema appena installato

```bash
whoami
uname -a
uname -m
cat /etc/os-release
cat /etc/nv_tegra_release
python3 --version
df -h /home/nvidia
```

L'architettura deve essere `aarch64`. Il percorso corrente del progetto richiede
Python 3.10 o successivo. Registra la release Jetson Linux e il kernel effettivamente
avviato: avere copiato un nuovo kernel non prova che il boot lo stia usando.
La procedura pacchetti seguente è pensata per Ubuntu/Jetson Linux con APT;
verifica disponibilità e versioni sulla release installata.

## 2. Ripristina SSH senza password dal Mac

**Jetson**, dal terminale locale o da una sessione con password:

```bash
sudo apt update
sudo apt install openssh-server rsync
sudo systemctl enable --now ssh
install -d -m 0755 /home/nvidia/codex-work
```

**Mac:** conserva l'alias SSH `jetson-codex` già configurato. Reinstalla sulla
Jetson la **chiave pubblica corrispondente all'identità usata da quell'alias**.
Se hai `ssh-copy-id`:

```bash
# Sostituisci il percorso con quello della tua chiave PUBBLICA.
ssh-copy-id -o BatchMode=no -i ~/.ssh/CHIAVE_JETSON.pub jetson-codex
```

L'alias locale può avere `BatchMode yes`, necessario alle campagne ma incompatibile
con la richiesta iniziale della password. `-o BatchMode=no` lo disabilita soltanto
per l'installazione della chiave: inserisci la password dell'utente `nvidia`
impostata dopo il flash, non la password del Mac.

Se `ssh-copy-id` non è disponibile, apri sulla Jetson
`~/.ssh/authorized_keys` e aggiungi la riga della chiave pubblica del Mac,
preservando le righe esistenti. Poi, **sulla Jetson come `nvidia`**:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

La directory e il file devono appartenere a `nvidia`. Se la chiave privata ha una
passphrase, rendila disponibile all'agent SSH del Mac prima del test.

**Mac**, verifica nella stessa sessione da cui lancerai il controller:

```bash
ssh -o BatchMode=yes jetson-codex 'id; python3 --version; command -v rsync'
```

Deve riuscire senza chiedere password. Il runner usa `BatchMode=yes`: un login
che funziona soltanto inserendo la password non basta. Se il flash ha cambiato
la host key, verifica la nuova impronta dal terminale locale della Jetson prima
di aggiornare la relativa voce in `known_hosts`. Non disabilitare il controllo
delle host key.

## 3. Installa i tool di misura

**Jetson:**

```bash
sudo apt install python3 bash util-linux rt-tests perl rsync
command -v python3 bash taskset cyclictest perl rsync
cyclictest --help
```

`taskset`, `lscpu` e `chrt` arrivano da `util-linux`; `cyclictest` da `rt-tests`;
Perl serve a generare i FlameGraph con gli script trasferiti dal controller.
Il runner legge direttamente `/proc` e `/sys` per la telemetria: `tegrastats` è
utile per controlli manuali, ma non è un prerequisito obbligatorio del worker.

Il `cyclictest` installato deve supportare **`--policy`, `--json` e `--histfile`**:
il runner usa tutti e tre. Se una versione non li supporta, installa una versione
di `rt-tests` che li includa prima di eseguire una campagna; i permessi non risolvono
un'opzione assente. La prova al punto 5 verifica anche questi argomenti.

## 4. Installa perf e rendilo utilizzabile senza sudo

### 4.1 Eseguibile perf sul kernel Tegra

```bash
command -v perf
perf --version
```

Se funziona già, passa al punto 4.2. Se manca, oppure compare
`perf not found for kernel ...-tegra`, puoi installare i tool Ubuntu:

```bash
sudo apt install linux-tools-common linux-tools-generic
find /usr/lib/linux-tools -type f -name perf -executable
```

Su un kernel personalizzato Tegra, `/usr/bin/perf` può essere un wrapper che
cerca un pacchetto con il nome esatto di `uname -r`, assente nei repository Ubuntu.
Usa allora l'**eseguibile reale** trovato sopra, scegliendo esplicitamente una
versione disponibile, preferibilmente della stessa serie del kernel:

```bash
# Sostituisci VERSIONE con la directory effettivamente trovata.
/usr/lib/linux-tools/VERSIONE/perf --version
sudo install -o root -g root -m 0755 \
    /usr/lib/linux-tools/VERSIONE/perf /usr/local/bin/perf
hash -r
command -v perf
perf --version
```

Questo installa una copia del binario, non cambia il kernel avviato. Verifica che
anche una shell SSH non interattiva trovi `/usr/local/bin/perf`. Una versione
Ubuntu diversa dal kernel NVIDIA è un candidato da validare con `stat`, `record`
e `script`, non una compatibilità garantita. Se fallisce, la soluzione preferibile
è compilare `tools/perf` **dai sorgenti NVIDIA della release/kernel usati per il
flash**, con supporto DWARF/libunwind, e installarne il binario nello stesso percorso.
Non installare un kernel generic per correggere il wrapper di perf.

### 4.2 Permessi persistenti

Il runner invoca direttamente `perf`, senza `sudo`. Una regola
`NOPASSWD: /usr/bin/perf` da sola **non lo sblocca**.
Per questa macchina di laboratorio abilita l'accesso agli eventi anche
system-wide e kernel:

```bash
sudo tee /etc/sysctl.d/99-jetson-campaign-perf.conf >/dev/null <<'EOF'
kernel.perf_event_paranoid = -1
EOF
sudo sysctl -p /etc/sysctl.d/99-jetson-campaign-perf.conf
cat /proc/sys/kernel/perf_event_paranoid
```

Il valore atteso è `-1`, persistente dopo il reboot. La scelta apre perf a tutti
gli utenti locali della Jetson; evita di combinarla con utenti non fidati.
Il significato dei livelli è descritto nella
[documentazione Linux di perf](https://www.kernel.org/doc/html/v5.15/admin-guide/perf-security.html).

Prova **senza sudo**, usando una CPU online:

```bash
cat /sys/devices/system/cpu/online
perf stat -a -C 0 -e cycles,instructions -- sleep 1
perf stat -e instructions:u,instructions:k -- sleep 1
ls /sys/bus/event_source/devices/armv8_cortex_a78/events
```

Il progetto verifica la PMU Cortex-A78AE e gli encoding esposti dal kernel.
`<not supported>`, eventi assenti o un nome PMU diverso richiedono una verifica
del nuovo kernel; non sono necessariamente problemi di autorizzazione.

Per i simboli kernel, se necessari al profiling/tracing e ancora nascosti:

```bash
cat /proc/sys/kernel/kptr_restrict
# Opzionale: rende visibili gli indirizzi kernel anche agli utenti locali.
echo 'kernel.kptr_restrict = 0' \
    | sudo tee /etc/sysctl.d/99-jetson-campaign-symbols.conf >/dev/null
sudo sysctl -p /etc/sysctl.d/99-jetson-campaign-symbols.conf
```

Questo non è richiesto per contare le istruzioni kernel con `instructions:k`.

## 5. Priorità FIFO e memoria bloccata per cyclictest

Imposta i limiti dell'utente, senza eseguire l'intera campagna come root:

```bash
sudo tee /etc/security/limits.d/99-jetson-campaign.conf >/dev/null <<'EOF'
nvidia soft rtprio 99
nvidia hard rtprio 99
nvidia soft memlock unlimited
nvidia hard memlock unlimited
EOF
```

`rtprio` permette la priorità FIFO 90 configurata nei YAML; `memlock` permette
`cyclictest -m`. I limiti si applicano alle **nuove sessioni PAM**, non al terminale
già aperto. Vedi il
[manuale limits.conf](https://man7.org/linux/man-pages/man5/limits.conf.5.html).

Esci dalla sessione e riconnettiti. Controlla:

```bash
ulimit -r
ulimit -l
chrt -f 90 /bin/true
OUT=$(mktemp -d /home/nvidia/codex-work/startup-check.XXXXXX)
taskset -c 0 cyclictest -a 0 -t 1 -p 90 --policy=fifo -m \
    -i 1000 -D 2 -h 1000 -q \
    --json="$OUT/cyclictest.json" --histfile="$OUT/cyclictest.hist"
ls -l "$OUT"
```

Attesi: `99`, `unlimited`, comandi riusciti e file prodotti. Per il controller
contano i limiti della sessione **SSH non interattiva**. Dal **Mac**:

```bash
ssh -o BatchMode=yes jetson-codex 'ulimit -r; ulimit -l; chrt -f 90 /bin/true'
```

Se qui risultano ancora `0` o un memlock basso, sulla Jetson verifica
`sudo sshd -T | grep usepam` (atteso `usepam yes`) e che lo stack PAM di SSH
carichi `pam_limits.so`, anche tramite i file `common-session*` inclusi.
Prima di modificare SSH conserva una sessione aperta; valida con `sudo sshd -t`
e poi usa `sudo systemctl reload ssh`. Riconnettiti e ripeti il controllo.
Non basta aumentare il limite nella sola shell locale.

Non è necessario disabilitare globalmente il throttling realtime
(`kernel.sched_rt_runtime_us`): conserva il valore del sistema per questo setup.

### Accesso a /dev/cpu_dma_latency

Se cyclictest produce i file ma stampa
`WARN: open /dev/cpu_dma_latency: Permission denied`, FIFO e memlock possono
essere già corretti: manca il permesso sul dispositivo PM QoS. Cyclictest
continua, ma non può applicare la propria richiesta di latenza CPU, che può
influenzare gli stati idle e quindi le latenze misurate.
La richiesta rimane attiva finché il relativo file descriptor resta aperto;
vedi la [documentazione Linux PM QoS](https://www.kernel.org/doc/html/latest/admin-guide/pm/cpuidle.html).

Sulla Jetson, assegna l'accesso all'utente di misura con una regola udev persistente:

```bash
sudo tee /etc/udev/rules.d/99-jetson-campaign-latency.rules >/dev/null <<'EOF'
SUBSYSTEM=="misc", KERNEL=="cpu_dma_latency", OWNER="nvidia", MODE="0600"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=misc --sysname-match=cpu_dma_latency
sudo udevadm settle
ls -l /dev/cpu_dma_latency
test -r /dev/cpu_dma_latency && test -w /dev/cpu_dma_latency && echo 'Accesso PM QoS OK'
```

Atteso: proprietario `nvidia`, permessi `crw-------`, accesso riuscito senza sudo.
Ripeti poi la prova cyclictest precedente: il warning deve scomparire.
La regola ripristina i permessi al reboot; non serve una nuova sessione perché
non cambia i gruppi dell'utente. Mantieni lo stesso accesso PM QoS tra le misure
che confronti: abilitare la richiesta può cambiarne le condizioni sperimentali.

## 6. Helper cpuset per le campagne con isolcpu

Serve se il YAML contiene `isolcpu` non vuoto. Installa la copia **del checkout
attuale**, anche se una copia precedente esisteva prima del flash.

**Mac**, dalla root di questo repository:

```bash
ssh jetson-codex 'mkdir -p /home/nvidia/codex-work/startup-orin'
scp tools/jetson-campaign-cgroup \
    jetson-codex:/home/nvidia/codex-work/startup-orin/
```

**Jetson:**

```bash
sudo install -o root -g root -m 0755 \
    /home/nvidia/codex-work/startup-orin/jetson-campaign-cgroup \
    /usr/local/sbin/jetson-campaign-cgroup

echo 'nvidia ALL=(root) NOPASSWD: /usr/local/sbin/jetson-campaign-cgroup *' \
    | sudo tee /etc/sudoers.d/jetson-campaign-cgroup >/dev/null
sudo chmod 0440 /etc/sudoers.d/jetson-campaign-cgroup
sudo visudo -cf /etc/sudoers.d/jetson-campaign-cgroup
sudo visudo -c

stat -fc %T /sys/fs/cgroup
cat /sys/fs/cgroup/cgroup.controllers
sudo -n /usr/local/sbin/jetson-campaign-cgroup probe --rtprio 90
```

Attesi: filesystem `cgroup2fs`, controller `cpuset`, probe riuscita. La probe
crea e rimuove un piccolo cpuset di controllo. L'helper usa la partizione
`isolated` se supportata, altrimenti `root`; il workload viene poi eseguito
come `nvidia`. Non aggiungere `NOPASSWD: ALL`.

Se manca cgroup v2 o cpuset, fermati per le campagne isolate: occorre verificare
configurazione e boot del kernel. `taskset` da solo non sostituisce quel requisito.

## 7. Tool NVIDIA e hook senza password

```bash
command -v tegrastats nvpmodel jetson_clocks
sudo nvpmodel -q
sudo jetson_clocks --show
```

Se mancano, controlla che l'installazione Jetson Linux/JetPack corrisponda al BSP
usato per il flash. Questi strumenti non si ripristinano installando `nvidia-smi`.
Mantieni lo stesso power mode e la stessa politica di clock nei confronti.
Gli ID `nvpmodel` dipendono dal modello e dalla release: non assumere che `-m 0`
abbia sempre lo stesso significato.

Per gli hook con `sudo -n jetson_clocks`, autorizza il percorso effettivo:

```bash
CLOCKS_BIN=$(readlink -f "$(command -v jetson_clocks)")
test -n "$CLOCKS_BIN" && test -x "$CLOCKS_BIN" && \
    printf 'nvidia ALL=(root) NOPASSWD: %s\n' "$CLOCKS_BIN" \
    | sudo tee /etc/sudoers.d/jetson-campaign-clocks >/dev/null
sudo chmod 0440 /etc/sudoers.d/jetson-campaign-clocks
sudo visudo -cf /etc/sudoers.d/jetson-campaign-clocks
sudo -n "$CLOCKS_BIN" --show
```

Esegui questo blocco solo se `jetson_clocks` esiste ed è root-owned, senza permessi
di scrittura per `nvidia`. La regola permette anche store/restore e modifica dei
clock. Nei YAML usa quel percorso assoluto se sudo o SSH non risolvono il nome.
Autorizza analogamente gli eventuali altri hook effettivamente configurati,
limitando eseguibile e argomenti. La campagna non usa automaticamente sudo per
perf, cyclictest o i comandi della demo.

## 8. Ftrace, trace-cmd ed eBPF

Questi strumenti sono aggiuntivi rispetto alle campagne perf/cyclictest:

```bash
sudo apt install trace-cmd bpftrace
bpftrace --version
command -v trace-cmd
```

Controlla **il kernel appena avviato**:

```bash
if test -r /proc/config.gz; then
    zcat /proc/config.gz
else
    cat "/boot/config-$(uname -r)"
fi | grep -E 'CONFIG_(PERF_EVENTS|HW_PERF_EVENTS|ARM_PMU|CGROUPS|CPUSETS|FTRACE|FUNCTION_TRACER|FUNCTION_GRAPH_TRACER|DYNAMIC_FTRACE|KPROBES|KPROBE_EVENTS|BPF|BPF_SYSCALL|BPF_EVENTS|DEBUG_INFO_BTF)='

findmnt -t tracefs
sudo mkdir -p /sys/kernel/tracing
mountpoint -q /sys/kernel/tracing || \
    sudo mount -t tracefs tracefs /sys/kernel/tracing
sudo cat /sys/kernel/tracing/available_tracers
```

Se né `/proc/config.gz` né `/boot/config-*` esistono, usa la configurazione
originale della build del kernel; l'assenza del file non prova che le funzioni
siano disabilitate. L'elenco con `grep` mostra solo le opzioni abilitate.

Per la guida ftrace serve `function`; per `function_graph` serve anche il relativo
tracer. Per le kprobe servono `CONFIG_KPROBES`, `CONFIG_KPROBE_EVENTS` e il supporto
BPF; verifica anche `DYNAMIC_FTRACE` e `available_filter_functions` per il listing
usato da bpftrace. BTF serve alle sonde che leggono tipi kernel, non a tutte le sonde.
Un mount o sudo non possono abilitare funzionalità escluse dalla build.

Per consentire bpftrace e trace-cmd senza password, **opzionale**:

```bash
# Percorsi tipici Ubuntu: verifica prima che siano quelli installati.
ls -l /usr/bin/bpftrace /usr/bin/trace-cmd
sudo tee /etc/sudoers.d/jetson-campaign-tracing >/dev/null <<'EOF'
nvidia ALL=(root) NOPASSWD: /usr/bin/bpftrace, /usr/bin/trace-cmd
EOF
sudo chmod 0440 /etc/sudoers.d/jetson-campaign-tracing
sudo visudo -cf /etc/sudoers.d/jetson-campaign-tracing
sudo visudo -c
sudo -n bpftrace -e 'interval:s:1 { printf("eBPF interval OK\n"); exit(); }'
sudo -n bpftrace -l 'kprobe:newidle_balance*'
```

Sono strumenti potenti eseguiti come root; questa delega è adatta all'utente
fidato della macchina di laboratorio. La sonda interval verifica soltanto quella
capacità, non prova l'attach alle kprobe. Le guide ftrace con scritture dirette
in tracefs continuano a richiedere una shell amministrativa (`sudo bash`, con
password iniziale): autorizzare `trace-cmd` non autorizza le redirezioni della
shell. Non rendere tutto tracefs scrivibile con `chmod -R 777`.

`bpftool` è distinto da `bpftrace` e non è necessario per la guida eBPF corrente.
Se ti serve, cerca il binario dei linux-tools installati o costruiscilo dai
sorgenti compatibili; non riutilizzare alla cieca il vecchio link alla versione
`5.15.0-194`, che dopo il flash potrebbe non esistere.

## 9. Prova il profiling e poi il doctor

**Jetson come `nvidia`, senza sudo**, dalla nuova sessione con i limiti corretti:

```bash
OUT=$(mktemp -d /home/nvidia/codex-work/perf-startup.XXXXXX)
perf record -a -C 0 -e cpu-clock -F 99 --call-graph dwarf,8192 \
    -o "$OUT/perf.data" -- sleep 2
perf script --no-inline -i "$OUT/perf.data" > "$OUT/perf-script.txt"
ls -lh "$OUT"
```

Devono riuscire sia la registrazione sia la decodifica. Se la versione di perf
non supporta DWARF o `--no-inline`, correggi l'installazione prima dei FlameGraph.

**Mac**, dalla root del checkout, usando l'ambiente Python del controller già
installato:

```bash
source .venv/bin/activate
type jetson_test
command -v jetson-tests
python -m jetson_tests doctor --config campaign/campaign.yaml
```

Usa il YAML che intendi realmente eseguire, incluso uno con `isolcpu` se ti serve
quella modalità. `doctor` verifica tool, PMU, CPU, FIFO, permessi perf e helper,
e salva i log locali; non avvia la campagna completa. La demo e gli interferenti
devono essere fermi durante il controllo. L'eseguibile INTERFGEN configurato
(`/home/nvidia/hesoc-mark/membench/meminterf` nei profili attuali) deve esistere
ed essere eseguibile, se lo scenario è abilitato.

Se `jetson_test` manca ma `python -m jetson_tests` funziona, manca soltanto il tuo
wrapper locale. Recupera la definizione precedente. Se il wrapper era un semplice
alias del CLI di questo progetto, puoi ricrearlo nel file di configurazione della
shell del **Mac** con `alias jetson_test='jetson-tests'`; gli argomenti del CLI
restano `doctor`, `plan`, `run`, `resume`, `report`. Se manca anche `jetson-tests`,
reinstalla dal checkout nel virtualenv con `python -m pip install -e .`.

Dopo un nuovo flash usa una **nuova campagna** per il nuovo kernel. Non trattare
un `resume` di misure precedenti come un confronto nello stesso ambiente.

## 10. A ogni avvio successivo

I pacchetti, sudoers, sysctl e limiti PAM restano installati; i limiti vengono
riletti al login. Verifica `perf_event_paranoid` dopo il reboot: altri file sysctl
potrebbero sovrascriverlo. Clock, power mode e mount tracefs vanno controllati;
il mount manuale può dover essere ripetuto se il sistema non lo prepara al boot.

1. Verifica accesso SSH in `BatchMode=yes` e limiti `rtprio`/`memlock`.
2. Controlla kernel, CPU online, power mode e clock rispetto al protocollo scelto.
3. Esegui `doctor` con il YAML della campagna prima delle misure.
4. Per tracing, controlla tracefs e disponibilità delle sonde del kernel corrente.

| Sintomo | Controllo/correzione |
|---|---|
| `Permission denied (publickey,password)` | Ripristina chiave pubblica e proprietario/permessi di `authorized_keys`. |
| `perf not found for kernel ...tegra` | Wrapper Ubuntu: usa il binario reale o perf compilato dai sorgenti NVIDIA. |
| `No permission to enable ...` | Verifica il valore effettivo di `perf_event_paranoid` e gli errori delle probe. |
| `Insufficient FIFO RLIMIT_RTPRIO` | Nuovo login SSH, PAM e `limits.d`; atteso `ulimit -r` almeno 90. |
| `mlockall failed` | Verifica `ulimit -l` nella sessione che esegue cyclictest. |
| `open /dev/cpu_dma_latency: Permission denied` | Installa la regola udev del punto 5 e ripeti cyclictest senza sudo. |
| `sudo: a password is required` | Regola sudoers per l'eseguibile effettivamente chiamato e controllo con `sudo -n`. |
| Helper/cpuset non disponibile | Installa l'helper root-owned e verifica cgroup v2 con cpuset. |
| PMU/eventi mancanti | Verifica il kernel nuovo e `/sys/bus/event_source/devices`, non solo sudo. |
| Kprobe o tracer assente | Verifica configurazione kernel e funzione disponibile; i permessi non bastano. |

Riferimenti ulteriori: [personalizzazione kernel NVIDIA](https://docs.nvidia.com/jetson/archives/r36.4.4/DeveloperGuide/SD/Kernel/KernelCustomization.html)
(se il BSP è R36.4.4; per altre release usa la documentazione corrispondente),
[guida ftrace del progetto](FUNCTION_TRACER.md),
[guida eBPF del progetto](EBPF_CYCLICTEST.md).
