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

## NOT ADOPTED (from v2.2)

Full reasoning lives here as decisions are revisited. Summary:

- Last-3-digits leak check: false positives; V5 uses digit-boundary matching.
- Dropping `messages.role` / `seq`: they are used by E-8 reconstruction.
- Merging `buyer_agent.py` and `buyer_cli.py`: kept split (strategy vs I/O).
- `historyLength` default 5: default is full history up to the clamp.
- V5b (numbers within +/-5% of private values): over-fits the eval.
- Heuristic bid-shading in `fallback_offer`: needs peer modeling; Phase 8.
