# A2A Negotiation Demo

Two independent agents, built separately, negotiate a freelance Godot job over
a minimal subset of the [A2A (Agent2Agent)](https://a2a-protocol.org)
protocol, built by hand over JSON-RPC 2.0 + HTTP.

- **Buyer**: CLI agent. Wants a small 2D inventory system for a Godot game.
- **Seller**: FastAPI A2A server on `127.0.0.1:8002`. Freelance Godot dev.
- They negotiate three issues: **price**, **delivery days**, **revisions included**.
- **LLM proposes, code decides**: the LLM drafts a counter-offer + rationale;
  every accept / walk-away / concession / band check is pure code.
  The LLM can never accept.

The wire protocol is pinned to **A2A 1.0** (`SendMessage`, `GetTask`,
`CancelTask`, `TASK_STATE_*`, `ROLE_*`); see `docs/A2A_PIN.md`. Internal
names stay short (`offer`, `working`, ...) and are mapped at the protocol
boundary.

## Quick start

```bash
cd a2a-demo
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env        # fill NVIDIA_API_KEY for a real LLM proposer
make check                  # fast tier (must stay under 10 s) + ruff + mypy
```

Integration tests for kill/restart and concurrency use a real fake-NIM
subprocess and live in the slow tier:

```bash
.venv/bin/python -m pytest -m slow      # S9, S10a/S10b, S14  (~90 s)
```

One optional live test (`-m live`) runs a single real NIM negotiation when
`NVIDIA_API_KEY` and `NIM_MODEL` are set in `.env`; it is skipped otherwise
and is the only test allowed to bypass the offline network guard:

```bash
.venv/bin/python -m pytest -m live
```

## Run a negotiation (two terminals)

```bash
# terminal 1 — seller
.venv/bin/python -m src.main_seller          # :8002

# terminal 2 — buyer
.venv/bin/python -m src.buyer_cli --profile profiles/buyer.json
```

The buyer prints one line per round (round, own offer, both utilities), then
a final line: `AGREED ...` / `END outcome=...`. A negotiation verifiably
completes with zero LLM calls too (fallback-only mode — the default when
`NIM_MODEL` is unset), which is what the offline evals and S1 measure.

Verified end-to-end from the shell (offline, fallback-only): round 0 →
round 8 → `AGREED {price:790, ...} U_buyer=0.51` over the real wire against
a real uvicorn subprocess; trace lines appear in `A2A_TRACE_PATH`.

`--resume` resends the stored message byte-identically after a crash
(section 7.1, E-33); the accept and every offer that started a turn are
persisted so replaying a messageId never runs a duplicate turn (D-8, D-16).

## Trace

Every message and turn lifecycle event is appended to `logs/trace.jsonl`
(one JSON line per event). Verbatim excerpt from a completed negotiation
(fallback mode, `t0 = 2026-11-12T18:43:17`):

```json
{"ts":"2026-11-12T18:43:17.861997+00:00","agent":"seller","round":0,"direction":"in","kind":"offer","severity":"info","task_id":"c3145a38-23a7-41ae-bbec-72640e6f82ae","terms":{"price":680,"delivery_days":7,"revisions":5}}
{"ts":"2026-11-12T18:43:17.907120+00:00","agent":"seller","round":1,"direction":"out","kind":"counter","severity":"info","task_id":"c3145a38-23a7-41ae-bbec-72640e6f82ae","terms":{"price":1130,"delivery_days":19,"revisions":1},"own_U":0.89,"target_U":0.89,"source":"fallback"}
{"ts":"2026-11-12T18:43:17.909229+00:00","agent":"buyer","round":0,"direction":"out","kind":"offer","severity":"info","task_id":"c3145a38-23a7-41ae-bbec-72640e6f82ae","terms":{"price":680,"delivery_days":7,"revisions":5},"own_U":0.9,"source":"fallback"}
```

`own_U` / `target_U` are quantized to 2 decimals, and the full concession
curve is never written (E-35). `own_history` and any private profile value
are never traced; the private profile lives only in its own process.

## Evaluation results (offline, fallback mode)

Reproduce with `.venv/bin/python evals/run_evals.py --all-offline --runs 10`.
Metrics from `evals/results/x*-fallback.jsonl`:

**X1 — beta grid (buyer_beta × seller_beta; agreement rate / joint utility)**

| buyer \ seller | 3.0 | 1.0 | 0.5 |
|---|---|---|---|
| **3.0** | 1.00 / 0.763 | 1.00 / 0.750 | 1.00 / 0.804 |
| **1.0** | 1.00 / 0.860 | 1.00 / 0.860 | 1.00 / 0.804 |
| **0.5** | 1.00 / 0.897 | 1.00 / 0.850 | 1.00 / 0.824 |

Nash product 0.110–0.173; surplus fairness 0.23–0.73 (example profiles are
asymmetric by construction: the seller concessions dominate).

**X4 — MAX_ROUNDS ∈ {4, 8, 12}**: 100% agreement in all arms (fallback
concessions converge fast); joint utility 0.80–0.91; latency < 0.13 s/round.

**X6 — BAND_LOW/BAND_HIGH ∈ {0.03, 0.05, 0.10}**: identical arms by
construction offline — the band gates the *LLM proposer* only; the lattice
fallback is exempt from V3. The meaningful arm is `--mode live`.

**X2 (LLM vs pure fallback), X3 (rationale on/off), X5 (model per side)**
are implemented and have not been run in this environment (no NIM key).
Each runs both arms of its comparison and writes one combined file, with
per-run token counts for the section 11 budget:

```bash
.venv/bin/python evals/run_evals.py --experiment x2 --mode live --runs 20
.venv/bin/python evals/run_evals.py --experiment x3 --mode live --runs 20
.venv/bin/python evals/run_evals.py --experiment x5 --mode live --runs 20 \
    --seller-model <seller-model-id> --buyer-model <buyer-model-id>
```

Results go to `evals/results/x2-compare.jsonl`, `x3-rationale.jsonl` and
`x5-models.jsonl`. Without a key they exit with an actionable message
instead of a traceback. The wiring (both sides, rationale suppression,
per-side model binding) is covered offline by `tests/test_evals.py`.

## Layout

```
src/            server, client, store, state machine, agents, negotiator, trace
tests/          fast tier (offline, ASGI loopback) + slow tier (subprocess)
evals/          payload corpus (injection + leak probes) + run_evals.py
docs/           A2A_PIN.md (spec freeze), DESIGN_NOTES.md, RUNBOOK.md
profiles/       private buyer/seller profiles (never on the wire)
```

## Definition of Done (vs the frozen plan, section 16)

| Item | State | Evidence |
|---|---|---|
| docs/A2A_PIN.md exists; diff logged "v2.2: A2A pin" | ✓ | `docs/A2A_PIN.md`, `docs/DESIGN_NOTES.md` ("v2.2: A2A pin") |
| test_smoke asserts E-56 factory/FakeLLM semantics before test_server | ✓ | `tests/test_smoke.py` (factory, FakeLLM semantics, inject_clock, loopback) |
| Fallback ≤ EPS_ROUNDING of target, all rounds, both profiles; E-54 covered | ✓ | `test_negotiator.py::test_fallback_within_eps_and_monotone_all_rounds[buyer/seller]`, `test_empty_history_skips_monotone_filter` |
| Full negotiation, NO LLM (S1, S2); pytest -m fast < 10 s | ✓ | `test_server.py::test_s1_...`, `test_s2_...` (238 fast tests in ~7 s) |
| LLM proposer both sides | ✓ | `tests/test_llm_e2e.py` (both agents, `source=="llm"`, task completes) |
| Every T1–T8 and R0–R14 (incl R4b, R6a–R6d), G1–G3, C1–C5 row tested | ✓ | `test_state_machine.py`, `test_server.py` (R0–R14/G/C parametrized + named) |
| Round semantics end-to-end (S1 round symmetry) | ✓ | `test_server.py::test_s1_...` asserts `0,2,4,6` (buyer) / `1,3,5,7` (seller) |
| Restart mid-task resumes (S9) | ✓ | `test_slow.py::test_s9_kill9_midturn_restart_resumes`, `test_resume.py` |
| Replay never runs a second turn; replies byte-identical minus timestamp (S7, S10) | ✓ | `test_server.py::test_s7_replay_is_byte_identical_and_runs_one_turn`, `test_r6b_...`, `S10a/S10b` both tiers, `test_regression.py::test_replay_...` |
| Cancel during a running turn never overwritten (S18) | ✓ | `S10`/`S18`-class tests, `test_regression.py::test_late_llm_answer_discarded_after_cancel` |
| A NIM outage is a normal failure; retry once; fallback (E-50) | ✓ | `test_llm.py`, `test_negotiator.py::two_invalid_proposals_fall_back`, fallback-only shell run verified |
| Agreement hash verified on every completed task | ✓ | `test_server.py::test_s1_...` asserts artifact pre/post the negotiation |
| Trace: severity + error_kind, quantized U, no own_history (E-35, E-51) | ✓ | `test_trace.py::test_*`, `test_security.py::test_s5_...`, `test_llm_e2e.py` token assertions |
| round_at_turn_start NULL on idle task; test passes | ✓ | `test_store.py::test_round_at_turn_start_is_null_when_idle` |
| CAS +2 on counter, +0 on accept/walk_away | ✓ | `test_store.py::test_cas_round_advance_plus_two`, `test_cas_accept_and_walk_away_do_not_advance_round` |
| S19 injected row sets round_at_turn_start; drifted round → RESUME_DRIFT | ✓ | `test_server.py::test_r6c_resume_drift_is_rejected`, `tests/test_resume.py`, `test_regression.py::test_resume_drift_is_rejected_with_32602` |
| X1–X6 saved in evals/results; summarized here | ~ X1/X4/X6 saved (offline); X2/X3/X5 implemented, live numbers pending NIM key | `evals/results/*.jsonl` (gitignored), `tests/test_evals.py`, this file |
| pytest zero network (except -m live); test_fast_tier_no_network.py enforces | ✓ | `conftest.py::_no_real_network`, `tests/test_fast_tier_no_network.py` |
| `make check` passes | ✓ | 238 fast + ruff + mypy, ~7 s |
| README, RUNBOOK, DESIGN_NOTES, A2A_PIN current | ✓ | this file; `docs/*` |
| Every §15 entry has a test_regression.py guard + runbook section | ✓ | `tests/test_regression.py` (12 tests, one per promoted bug); RUNBOOK "Section 15 promotions" |

Open items pending a NVIDIA NIM key (not in this environment):
- X2 LLM-vs-fallback, X3 rationale on/off, X5 per-side models — implemented with combined result files and per-run token counts; run each with `--mode live` (see the Evaluation results section).

## Status & known limitations

Working: full wire protocol (offer/counter/accept/walk_away), idempotency,
restart recovery, concurrency safety, injection/leak handling (V5/V6) with
quantized trace (incl. `tokens_in`/`tokens_out` per outbound event),
buyer `--resume`, LLM-vs-LLM e2e test (`tests/test_llm_e2e.py`), scenario
tests S1, S2, S3, S5, S6, S9, S10, S14, S16, S17, S19, S20, and
table-driven coverage of the R/G/C/S4 rows.

Known limitations (accepted, documented in `docs/DESIGN_NOTES.md`):
- negotiation is one-shot linear; no revision/branch support
- `fallback_offer` cannot reason about the peer's utility (no bid-shading)
- X2/X3/X5 live comparisons not yet run (need a NIM key; the runner is
  implemented and fails fast without one); offline lattice agreement rate
  is 1.00 by construction, so offline results don't show "the LLM's edge"
- `Decision`-level token accounting only covers the agents' own LLM calls;
  it does not capture R3/parse failures before a proposal is built
- equal boulware deadlock is possible with symmetric curves at high betas
  (documented; `END_U` at round T breaks it)
