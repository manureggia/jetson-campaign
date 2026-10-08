# Provenienza e riuso

La repository è indipendente dalla directory superiore a runtime.

- `jetson_tests/legacy_parsers.py`: estratti `percentile`, `irq_kind`,
  `parse_interrupt_snapshot` da `scripts/analyze_results.py` della directory originale.
- `jetson_tests/flamegraph.py`: estratti `HEADER`, `blocks`, `core_profile_quality`
  da `scripts/demo_flamegraph.py`.
- `jetson_tests/metrics.py`: conserva il formato perf CSV già raccolto dal vecchio
  runner, con matching esatto, interi precisi, momenti dell'istogramma e IRQ invalidi espliciti.
- `jetson_tests/processes.py`: sviluppa l'approccio `start_new_session`/`killpg`
  già presente in `scripts/run_timer_diagnostics.py`, aggiungendo identità persistenti,
  limiti e verifica dei discendenti.
- `tools/FlameGraph`: script Perl già inclusi nel progetto originale, upstream
  commit `41fee1f99f9276008b7cd112fca19dc3ea84ac32`. Provenienza e licenza CDDL
  sono conservate in `SOURCE.md`, `LICENSE` e nei file stessi.
- `tests/fixtures/cyclictest.json` e `perf_victim_cpu.csv`: copie dei raw della
  campagna `20260902_121006/BASELINE/CPU0/run01/pass_core`. Sono dati storici,
  non nuove misurazioni effettuate durante lo sviluppo.

Le sorgenti originali e tutti i risultati precedenti sono rimasti intatti.

## Impronte delle sorgenti originali al momento del riuso

- `analyze_results.py`: `d99f6b8c5bb18b18310b7559bf9b00c1fd69721309165a393dc69fb9db7fdacb`
- `demo_flamegraph.py`: `60e4abd50e36f192ab6e702dea4f7b2d71f1a4863ca8bc4cfb86b947bd889246`
- `run_timer_diagnostics.py`: `e285298c5972d4d884fb6d03c8f91425b9c13181368155cb3b12d2ed0bdcee0b`
