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

## Live experiments (X2/X3/X5) and the `live` marker

`-m live` is the only marker allowed to reach an external host; `conftest.py`'s
E-53 guard exempts live-marked tests and blocks everything else.
`tests/test_live.py` runs one real NIM negotiation (skipped without a key).

X2/X3/X5 need a real NIM key and are never part of `make check`:

```bash
.venv/bin/python evals/run_evals.py --experiment x2 --mode live --runs 20
.venv/bin/python evals/run_evals.py --experiment x3 --mode live --runs 20
.venv/bin/python evals/run_evals.py --experiment x5 --mode live --runs 20 \
    --seller-model <id> --buyer-model <id>       # or NIM_MODEL + NIM_MODEL_B
```

Each writes ONE combined file so both arms sit side by side:
`evals/results/x2-compare.jsonl`, `x3-rationale.jsonl`, `x5-models.jsonl`.
Every run records `tokens_in`/`tokens_out`; compare against the section 11
budget (~12k tokens per negotiation).

Trap door: without `NVIDIA_API_KEY` (or without a model id) the runner
exits with `live mode needs NVIDIA_API_KEY ...` / `... needs a model id`
instead of a traceback. X2/X3 also refuse `--mode fallback` explicitly:
their comparison needs a real proposer. X5 refuses identical model ids.

Offline guards for the wiring (no network, no key):
`tests/test_evals.py` injects `FakeLLM` callables into both sides, checks
the token ledger, the per-call model binding, and that
`include_rationale=False` produces data-only messages.

### Peer text never reaches the seller's LLM
`_begin_turn` stripped the message down to its data part before spawning
the turn, so the proposer prompt saw no peer rationale (the resume path
attached `_peer_text` correctly — only the normal path dropped it). Fix:
`_begin_turn` attaches the text part as `_peer_text` like the resume
path does. Regression test:
`tests/test_regression.py::test_peer_text_reaches_responder_on_normal_turn`.

### Replay of an accept errors instead of returning the task
The buyer's accept message was never stored (R11 only wrote tasks +
artifact), so a replay fell through to R7 (-32602 terminal). Fix:
`store.complete_with_accept()` stores the accept message in the SAME
transaction as the completion, so D-8 replay returns the task (R6b).
Regression test:
`tests/test_regression.py::test_replay_of_accept_returns_the_completed_task`.

### The sync LLM call freezes the whole seller process
`llm.chat()` is synchronous; called directly from the async turn task it
monopolized uvicorn's event loop and also broke the
`asyncio.wait_for(..., BLOCKING_TIMEOUT)` snapshot — stalling
tasks/get, tasks/cancel AND the reply. Fix: run the responder in a
worker thread (`asyncio.to_thread`). Regression test:
`tests/test_regression.py::test_sync_responder_does_not_block_event_loop`.

### The seller runs fallback-only without ever calling the LLM
`main_seller.py` created the app without `chat=`, so the default
responder always used the fallback, regardless of env credentials. Fix:
`build_app()` passes `llm.default_chat()` (None => explicit fallback-only
mode). Guard: `tests/test_llm.py` (`test_build_app_*`). The slow tier
itself breaks without this: scripted fake-NIM responses never get used.

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
  Gate: `pytest -m fast` is ~240 tests in ~7 s; S1 completes with a verified
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
  Remaining for full Phase 6: eval runner exists, and the README now
  summarizes the offline X1/X4/X6 results (agreements 1.00 by construction
  on the lattice fallback). X2/X3/X5 stay `--mode live` (need a NIM key).
  `tests/test_llm_e2e.py` now covers a full negotiation with the LLM
  proposer on both sides (source == "llm" in all outbound trace events).
- Phase 6 (partial): slow tier exists. `tests/fake_nim_server.py` — real
  HTTP fake of the NIM chat API (`/__stage`, `/__control`, `/__reset`,
  `/__state`, port printed to stdout). `tests/test_slow.py` — S9
  (kill -9 mid-turn, restart, R6c resume, no duplicated messages),
  S10a/S10b (concurrency; the S10a loser may see R6b or R6d while its
  turn is in flight), S14 (both LLM attempts die at LLM_TIMEOUT, snapshot
  reply carries the task id at BLOCKING_TIMEOUT, turn completes afterwards).
  `tests/test_fast_tier_no_network.py` + the autouse `_no_real_network`
  fixture in conftest block every non-localhost socket across the whole
  suite (E-53). Four production bugs found and fixed via this tier (see
  the trap doors above). `make check` stays fast-tier-only; slow tier is
  `make slow` (~90 s).
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

## Section 15 promotions

Catalog of past bug classes from the frozen plan (section 15) mapped to
their guards. Runbook + `tests/test_regression.py` land in the same commit
as the fix, per the standing rule.

- "LLM acts like a pushover: the band (V3) is the cure" -> V3 in
  `validate_proposal` (V3 before V4, E-29); named guard:
  `tests/test_regression.py::test_pushover_counter_rejected_by_band`. The
  LLM can never accept; D-5.
- "U drifts off target" -> deterministic lattice at target(T) with
  EPS_ROUNDING 0.03; `tests/test_negotiator.py::test_fallback_within_eps_and_monotone_all_rounds`
  iterates every round for both example profiles. Do not loosen EPS.
- "Duplicate turns after a timeout" -> always retry with the same
  messageId; global uniqueness on the seller (D-16); R6b replay:
  `tests/test_server.py::test_r6b_replay_is_idempotent_with_and_without_task_id`.
- "Cancel then a late LLM answer overwrites canceled" -> CAS discard (D-15);
  named guard: `tests/test_regression.py::test_late_llm_answer_discarded_after_cancel`
  (cancelled state survives; `kind=discarded` traced).
- "Replay returns an error instead of the task" -> R6b returns the task;
  see `test_server.py::test_r6b_replay_is_idempotent_with_and_without_task_id`.
- "Equal boulware deadlock" -> expected; END_U at round T breaks it; X1
  documents it.
- "Spec drift: keep A2A names only in config.py and protocol.py" ->
  `tests/test_regression.py::test_spec_names_stay_in_pinned_modules`.
- "Poll sees `working` forever" -> see the E-22 and T8 entries above.
- "Round symmetry breaks at the buyer's second offer" -> see the
  `round symmetry` trap door and `tests/test_store.py::test_cas_round_advance_plus_two`.
- "Resume fails with 'resume round drift'" -> see the resume-drift entry.
- "Resume sends a re-derived message and trips R9" -> E-33 byte-identical
  resend; `tests/test_resume.py::test_resume_resends_stored_message_byte_identically`.
- "A NIM outage crashes a turn" -> LLMError is a normal failure; retry once
  then fallback; `tests/test_llm.py::test_chat_maps_*` +
  `tests/test_negotiator.py::two_invalid_proposals_fall_back`.
- "Rare S4 traffic mistakes" -> row tests named `test_r*` / `test_g*` /
  `test_c*` in `tests/test_server.py` and `tests/test_state_machine.py`.
- "Fallback proposals break monotone" -> V4 validator;
  `tests/test_negotiator.py::test_fallback_within_eps_and_monotone_all_rounds`.

- "Agent Card cached stale: restart the client" -> the client does not
  cache the card; the seller ships it fresh per request; avoid long-lived
  client processes against a re-configured seller (`fetch_card()` at the
  start of each run).
- "uvicorn --reload loses in-memory locks" -> dev only: run without
  `--reload`; the per-task locks are process-local.
- "S10a flakes under capacity load" -> use the MAX_OPEN_TASKS override and
  accept server_busy (or R6d) as valid loser outcomes;
  `tests/test_server.py::test_r4_capacity_and_r4b_concurrent_creators`.
- "Fallback proposals break monotone" -> V4;
  `tests/test_regression.py::test_fallback_stays_within_eps_of_target`.
- "Replay runs a second turn" ->
  `tests/test_regression.py::test_replay_never_reruns_finishing_turn`
  (three identical sends produce identical tasks/history).
- "Resume event traveler round drift" ->
  `tests/test_regression.py::test_resume_drift_is_rejected_with_32602`
  (drifted round_at_turn_start -> RESUME_DRIFT).
- "Round symmetry off-by-one at counter" ->
  `tests/test_regression.py::test_counter_advances_round_by_two_on_counter`
  and `tests/test_store.py::test_cas_round_advance_plus_two`.
- "Private number in outbound rationale" ->
  `tests/test_regression.py::test_v5_private_value_leak_blocked` (V5) and
  `tests/test_negotiator.py::test_v5_leak_phrase_and_exact_private_value`.
