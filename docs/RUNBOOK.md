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

### Peer text never reaches the seller's LLM
`_begin_turn` stripped the message down to its data part before spawning
the turn, so the proposer prompt saw no peer rationale (the resume path
attached `_peer_text` correctly — only the normal path dropped it). Fix:
`_begin_turn` attaches the text part as `_peer_text` like the resume
path does. Regression test:
`tests/test_regression.py::test_peer_text_reaches_responder_on_normal_turn`.

### S17 assertion: the profile prompt contains the private values
`prompts._profile_payload` includes `ideal`/`reservation`, so the retry
prompt for a leaky-then-clean proposer ALWAYS contains the digits (e.g.
`700`). Assert on the rejected *text* (rationale, "my reservation") and the
presence of `<retry_reason>V5 private value leak</retry_reason>`, never on
the absence of private digits in the prompt.

## Phase status

- Phase 0: skeleton, `src/config.py`, `src/clock.py`, `tests/conftest.py`,
  `tests/test_smoke.py`, `tests/test_config.py`.
- Phase 0.5: pinned in `docs/A2A_PIN.md`. The wire uses A2A 1.0 names:
  `SendMessage`, `GetTask`, `CancelTask`, `TASK_STATE_*`, `ROLE_*`, no
  `kind` discriminator. Internal names stay short.
- Phase 1: wire models, state machine, utility/negotiator, SQLite store with
  the CAS commit, seller server, client, buyer CLI and fallback-only agents.
  Gate: `pytest -m fast` is 195 tests in ~3 s; S1 completes with a verified
  artifact; S2 ends `rejected` with no artifact; both report zero LLM calls.
- Phase 2a/2b: per-task and creation locks released before awaiting the turn,
  R5-R14, replay/resume (R6a-R6d), G/C tables, CAS +2/+0 bookkeeping,
  startup recovery, turn-failure commit, client retry, first-message-timeout
  task id. S3, cancel-during-turn, S10a/S10b and restart-resume pass.
- Phase 4 (partial): `src/trace.py` — append-only JSONL, `own_U`/`target_U`
  quantized to 2 decimals, `severity`/`error_kind` (E-51), `own_history`
  never written (E-35). Entry points (`main_seller`, `buyer_cli`) configure
  it from `A2A_TRACE_PATH`; tests stay silent by default. Discarded CAS
  results and `turn_failed` are traced. `src/buyer_store.py` keeps the
  buyer `resume_state` table; `buyer_cli --resume` resends the stored
  message byte-identically (E-33) and clears the row on terminal outcomes.
  Peer text now reaches the responder on normal turns too (regression:
  `test_peer_text_reaches_responder_on_normal_turn`).
  Remaining for full Phase 4/5/6: slow-tier fake NIM + S9/S10/S14,
  eval runner, README + DoD sweep.
- Phase 5 (partial): security scenario tests `tests/test_security.py` —
  S5 (rationales carry no private price values/banned phrases across a full
  negotiation), S6 (all `evals/injection_payloads.json` payloads incl.
  NFKC-fullwidth + zero-width variants: dropped from the proposer prompt,
  never accepted, fallback sent, `error_kind=injection_attempt` logged),
  S17 (retry prompt excludes the rejected rationale; see the S17 note
  above), S20 (bearer on POST routes only, card GET public). Leak corpus:
  `evals/leak_probes.json`.

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

### A second message blocks instead of getting R8/R6d
The per-task (or creation) lock is being held across `await` of the turn.
Commit R14 and spawn the turn under the lock, then release it and await the
turn outside. Concurrent requests must see `working` immediately.

### Replay returns an error instead of the task
R6b must return the current Task, not an error.

### Poll sees `working` forever
An in-process turn exception must CAS-commit `failed` (E-22). After a
process death, startup recovery (T8) repairs it on restart.

### A NIM outage crashes a turn
`LLMError` is a normal failure (E-50): retry once, then fallback. A
`turn_failed` caused by a network error is a bug.
