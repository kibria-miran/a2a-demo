# Runbook

Dev-loop commands, trap doors, and fixes. Maintained under the standing rule
(E-57): every section 15 discovery gets a section here and a matching
`tests/test_regression.py::test_<slug>` in the SAME commit.

## Setup

```bash
cd /home/kibria/a2a-demo
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env   # then fill NVIDIA_API_KEY
```

## The gate

```bash
make check             # pytest -m fast && ruff check src tests && mypy src
make fast              # pytest -m fast only (must stay under 10 s)
make slow              # pytest -m slow only (S9, S10, S14)
make all               # pytest (fast + slow)
```

## Phase status

- Phase 0: skeleton, `src/config.py`, `src/clock.py`, `tests/conftest.py`,
  `tests/test_smoke.py`, `tests/test_config.py`.
- Phase 0.5: pinned in `docs/A2A_PIN.md`. The wire uses A2A 1.0 names:
  `SendMessage`, `GetTask`, `CancelTask`, `TASK_STATE_*`, `ROLE_*`, no
  `kind` discriminator. Internal names stay short.
- Phase 1 starts only after `pytest -m fast` passes.

## Trap doors

### Fast tier creeping past 10 s
A fast test is touching a real DB file or WAL. Use `tmp_path` and
`journal_mode=MEMORY` (E-43). Never loosen the 10 s gate.

### Resume fails with "resume round drift"
`round_at_turn_start` is NULL, or startup recovery did not set it to
`tasks.round`. Check T8 and E-47.

### Round symmetry breaks at the buyer's second offer
The CAS commit did not advance `tasks.round` by 2 on a counter. See
`test_cas_round_advance_plus_two` and S1 (E-47).

### "database is locked"
One writer transaction per step; WAL in the slow tier / production;
`busy_timeout` 5000 ms.

### Slow test monkeypatch does nothing
The seller is a subprocess. Use `tests/fake_nim_server.py` (E-37).

### Replay returns an error instead of the task
R6b must return the current Task, not an error.

### Poll sees `working` forever
An in-process turn exception must CAS-commit `failed` (E-22). After a
process death, startup recovery (T8) repairs it on restart.

### A NIM outage crashes a turn
`LLMError` is a normal failure (E-50): retry once, then fallback. A
`turn_failed` caused by a network error is a bug.
