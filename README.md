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

`--resume` resends the stored message byte-identically after a crash
(section 7.1, E-33); the accept and every offer that started a turn are
persisted so replaying a messageId never runs a duplicate turn (D-8, D-16).

## Trace

Every message and turn lifecycle event is appended to `logs/trace.jsonl`
(one JSON line per event):

```json
{"ts":"2026-11-12T17:57:16Z","agent":"seller","round":1,"direction":"out","kind":"counter","severity":"info","task_id":"…","terms":{"price":1130,"delivery_days":19,"revisions":1},"own_U":0.89,"target_U":0.89,"source":"fallback"}
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
require a real NIM key and have not been run in this environment. With the
key present, `python evals/run_evals.py --experiment x2 --mode live` runs
this comparison for real.

## Layout

```
src/            server, client, store, state machine, agents, negotiator, trace
tests/          fast tier (offline, ASGI loopback) + slow tier (subprocess)
evals/          payload corpus (injection + leak probes) + run_evals.py
docs/           A2A_PIN.md (spec freeze), DESIGN_NOTES.md, RUNBOOK.md
profiles/       private buyer/seller profiles (never on the wire)
```

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
- X2/X3/X5 live comparisons not yet run (need a NIM key); offline lattice
  agreement rate is 1.00 by construction, so offline results don't show
  "the LLM's edge"
- `Decision`-level token accounting only covers the agents' own LLM calls;
  it does not capture R3/parse failures before a proposal is built
- equal boulware deadlock is possible with symmetric curves at high betas
  (documented; `END_U` at round T breaks it)
