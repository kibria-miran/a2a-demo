# Design Notes

Changelog and rejected alternatives for the A2A negotiation demo (C3).

## v2.2: A2A pin

Phase 0.5 artifact: `docs/A2A_PIN.md`.

The live A2A specification is now **1.0.0**, not the v0.3-style surface
assumed by the v2.2 plan. The pin was taken on 2026-11-12 against
<https://a2a-protocol.org/latest/specification/> (content SHA-256
`3f0ab47bd7f206c83835c16face4d607a54ee413fb7835b07ee1696517e31d52`).

Diffs applied, restricted by E-55 to `src/config.py`, `src/protocol.py`
(Phase 1) and section 8.1 of the plan:

- JSON-RPC methods renamed: `message/send` -> `SendMessage`,
  `tasks/get` -> `GetTask`, `tasks/cancel` -> `CancelTask`.
- The `kind` discriminator was removed from all objects. Parts are
  `{"text": ...}` or `{"data": ...}`.
- Wire roles are `ROLE_USER` / `ROLE_AGENT`; wire task states are
  `TASK_STATE_*`. Internal database/state-machine names remain the short
  v2.2 names (`user`, `agent`, `working`, `input-required`, ...), mapped at
  the protocol boundary.
- `SendMessage` returns `result: {"task": ...}` (or `{"message": ...}`).
- The Agent Card requires `supportedInterfaces`; `protocolVersion` lives on
  each `AgentInterface` and is pinned to `1.0`.
- `configuration.blocking` maps to `configuration.returnImmediately`
  (default `false`, i.e. blocking).
- Clients send `A2A-Version: 1.0`.
- Error constants now cover the full `-32001`..`-32009` A2A range in
  addition to standard JSON-RPC codes.

No other plan section was revised during the pin. The spec is frozen at the
hash recorded in `docs/A2A_PIN.md`; do not re-read it during the build.

## Post-freeze: live hosted models and timeout overrides (2026-11-13)

- `src/config.py` now reads `A2A_LLM_TIMEOUT`, `A2A_BLOCKING_TIMEOUT`,
  `A2A_HTTP_TIMEOUT` and `A2A_POLL_MAX` from the environment. Defaults are the
  frozen v2.2 values (30/45/60/60); the S14 slow test and
  `test_config.py::test_timeout_ordering` both rely on the defaults and stay
  deterministic. Per-deployment overrides exist because the frozen budgets are
  hostile to slow free-tier hosted models (Nemotron Ultra measured 38-86 s per
  proposer call) and were explicitly approved by the user for live runs.
  Do NOT put the overrides in `.env` if you want the slow tier to stay at
  frozen values; pass them inline on the command line instead (see README).
- OpenRouter gate: `thinkingmachines/inkling-2-small:free` is only available on
  approved agentic harnesses; direct API calls are refused (403). The key in
  `.env` therefore points at `nvidia/nemotron-3-ultra-550b-a55b:free`. Free
  tier rate limits (429) fire intermittently; the negotiation still completes
  via the deterministic fallback by design (D-13).
- `llm.chat()` caps responses at `_MAX_TOKENS = 2048` (the reasoning free-tier
  model otherwise thinks past any small cap and returns an empty rationale
  that fails to parse).

## Post-freeze implementation notes (2026-11)

Decisions and bugs fixed after the v2.2 freeze, logged here per the changelog
convention (C3). Frozen sections (method names, decision tables, state
machine, round bookkeeping) were NOT revised; these are implementation
corrections only:

- R11 (buyer accept) now stores the incoming user message in the same
  transaction as task completion. R6b replay of an accept returns the
  completed task (D-8). Previously accepts left no user-row trail and
  replay fell through to R7.
- The turn is spawned via `asyncio.to_thread(responder, ...)`:
  `llm.chat()` is synchronous; the event loop was being monopolized by
  the proposer (starving `tasks/get`/`tasks/cancel`/`BLOCKING_TIMEOUT`),
  violating E-4's premise.
- `main_seller` passes `llm.default_chat()` (None => fallback-only
  mode); an app built without `chat=` would silently never call the LLM.
- Peer text part reaches the responder on normal turns (it was previously
  attached only on the resume path).
- Buyer `--resume` resends the stored `last_message_json` byte-identically
  (E-33); the buyer DB adds `resume_state`.
- Round symmetry: the CAS commit advances `tasks.round` by exactly 2 on a
  counter (E-47) — implementation conforms; guarded by
  `test_cas_round_advance_plus_two` and the S1 test.
- In `test_llm_e2e.py` both agents negotiate LLM-vs-LLM on a scripted
  FakeLLM; the negotiation completes and `source == "llm"` for all
  outbound agent messages (no fallback consumed).
- X2/X3/X5 were advertised in `evals/run_evals.py` but X2 was a print stub
  and X3/X5 were absent from the CLI. Implemented now: X2 runs fallback vs
  LLM-on-both-sides; X3 adds an `include_rationale` switch at the protocol
  boundary (`create_app(..., include_rationale=...)` and
  `negotiate(..., include_rationale=...)`) so outbound text parts can be
  suppressed; X5 adds an optional per-call `model` to `llm.chat()` and
  `llm.default_chat(model=...)`, with no global config mutation. Each
  writes a combined result file (`x2-compare`, `x3-rationale`,
  `x5-models`) and records per-run tokens. Live numbers still require a
  NIM key; the arms fail fast with an actionable message without one.
  Offline wiring guards live in `tests/test_evals.py`.

## v2.2: S16 clarification

S16 (mirror utilities) could not be satisfied as written: the assertion
required both utilities in `[END_U - 0.03, END_U + BAND_HIGH + EPS_BAND]`,
but the example mirror constructions either complete instantly (U_b = U_s
= 0.90 with a literal same-profile run) or land at U ~ 0.43-0.57 (exact
mirrors). A test forcing this: constructing mirror profiles reveals that
with identical weights/beta and flipped directions, the utilities are
exactly complementary (`U_b(t) + U_s(t) == 1` for all t), so any completed
deal sits at U ~ 0.5, far above the old window.

The spec line in 13.1 S16 (E-28) is replaced with the satisfiable
assertion: completed -> both utilities >= END_U - EPS_ROUNDING and differ
by < 0.03; otherwise rejected with reason `max_rounds`. Rationale is used
by `tests/test_server.py::test_s16_mirror_utilities_deal_lands_near_fifty_fifty` (beta 1.5,
deal closes at round 6 at U ~ 0.50 / 0.50).

Nothing else in the frozen plan was revised.

## NOT ADOPTED (from v2.2)

Full reasoning lives here as decisions are revisited. Summary:

- Last-3-digits leak check: false positives; V5 uses digit-boundary matching.
- Dropping `messages.role` / `seq`: they are used by E-8 reconstruction.
- Merging `buyer_agent.py` and `buyer_cli.py`: kept split (strategy vs I/O).
- `historyLength` default 5: default is full history up to the clamp.
- V5b (numbers within +/-5% of private values): over-fits the eval.
- Heuristic bid-shading in `fallback_offer`: needs peer modeling; Phase 8.
