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
- Live evaluations (X2/X3/X5) are not runnable without a NIM key; offline
  lattice runs are deterministic and 100% agreement by construction.

## NOT ADOPTED (from v2.2)

Full reasoning lives here as decisions are revisited. Summary:

- Last-3-digits leak check: false positives; V5 uses digit-boundary matching.
- Dropping `messages.role` / `seq`: they are used by E-8 reconstruction.
- Merging `buyer_agent.py` and `buyer_cli.py`: kept split (strategy vs I/O).
- `historyLength` default 5: default is full history up to the clamp.
- V5b (numbers within +/-5% of private values): over-fits the eval.
- Heuristic bid-shading in `fallback_offer`: needs peer modeling; Phase 8.
