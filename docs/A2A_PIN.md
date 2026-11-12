# A2A Spec Pin

Phase 0.5 artifact (E-55). This file freezes the A2A specification revision
used by this project. Do not re-read the A2A spec during the build.

## Pinned revision

| Item | Value |
|---|---|
| Latest released version | `1.0.0` |
| Canonical spec URL | https://a2a-protocol.org/latest/specification/ |
| Versioned spec URL | https://a2a-protocol.org/v1.0.0/specification |
| Retrieved (UTC) | 2026-11-12 |
| SHA-256 of the fetched "latest" HTML | `3f0ab47bd7f206c83835c16face4d607a54ee413fb7835b07ee1696517e31d52` |
| SHA-256 of the canonical URL string | `73e2793d3acb2dc410c6444cbd3b78d6f5deafa72d5aadc41e6a361733a5e356` |
| Protocol version pinned in `src/config.py` | `A2A_PROTOCOL_VERSION = "1.0"` |

The `A2A-Version` service parameter/header value is `1.0` (major.minor).

## Exact discovery surface

- `GET /.well-known/agent-card.json` (well-known URI suffix `agent-card.json`).
- The card is public metadata; only the JSON-RPC POST route is authenticated.
- `AgentCard` required fields: `name`, `description`, `supportedInterfaces`,
  `version`, `capabilities`, `defaultInputModes`, `defaultOutputModes`,
  `skills`.
- `AgentInterface` required fields: `url`, `protocolBinding`, `protocolVersion`.
  The JSON-RPC binding value is `JSONRPC`.

## Exact JSON-RPC method names

| Operation | JSON-RPC method | Plan's v0.3-style name |
|---|---|---|
| Send message | `SendMessage` | `message/send` |
| Get task | `GetTask` | `tasks/get` |
| Cancel task | `CancelTask` | `tasks/cancel` |

All requests use JSON-RPC 2.0: `{"jsonrpc":"2.0","id":...,"method":...,"params":...}`.

- `SendMessage` params: `SendMessageRequest` = optional `tenant`,
  required `message`, optional `configuration`, optional `metadata`.
- `SendMessage` result: `SendMessageResponse`, one of `{"task": Task}` or
  `{"message": Message}`.
- `GetTask` params: required `id`, optional `historyLength`, optional `tenant`.
- `CancelTask` params: required `id`, optional `tenant`.
- Unknown methods return `-32601`.

## Exact task states, roles, parts

- TaskState wire values:
  `TASK_STATE_UNSPECIFIED`, `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`,
  `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`,
  `TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_REJECTED`,
  `TASK_STATE_AUTH_REQUIRED`.
- Role wire values: `ROLE_UNSPECIFIED`, `ROLE_USER`, `ROLE_AGENT`.
- `Part` has **no `kind` discriminator** in v1.0 (see Appendix A.2.1). A part
  MUST contain exactly one of `text`, `raw`, `url`, `data`; `metadata`,
  `filename`, `mediaType` are optional.
- `Message` required fields: `messageId`, `role`, `parts`; optional
  `contextId`, `taskId`, `metadata`, `extensions`, `referenceTaskIds`.
- `Task` required fields: `id`, `status`; optional `contextId`, `artifacts`,
  `history`, `metadata`.
- `TaskStatus` required field: `state`; optional `message`, `timestamp`.
- `Artifact` required fields: `artifactId`, `parts`; optional `name`,
  `description`, `metadata`, `extensions`.

## Exact error codes

Standard JSON-RPC:

| Code | Name |
|---|---|
| `-32700` | JSONParseError |
| `-32600` | InvalidRequestError |
| `-32601` | MethodNotFoundError |
| `-32602` | InvalidParamsError |
| `-32603` | InternalError |

A2A-specific (from section 5.4):

| Code | Error type |
|---|---|
| `-32001` | TaskNotFoundError |
| `-32002` | TaskNotCancelableError |
| `-32003` | PushNotificationNotSupportedError |
| `-32004` | UnsupportedOperationError |
| `-32005` | ContentTypeNotSupportedError |
| `-32006` | InvalidAgentResponseError |
| `-32007` | ExtendedAgentCardNotConfiguredError |
| `-32008` | ExtensionSupportRequiredError |
| `-32009` | VersionNotSupportedError |

Error object: `{"code": int, "message": str, "data": [...]}`; each `data`
entry includes a `@type` key.

## Diffs against the v2.2 plan (v0.3-style names)

Per E-55 these diffs were applied ONLY to `src/config.py`, `src/protocol.py`
(when written in Phase 1) and section 8.1 of the plan. No other section was
revised. The internal short names used by the database, state machine and
tests are unchanged; `protocol.py` maps them at the wire boundary.

| Plan wording | Pinned v1.0 reality | Resolution |
|---|---|---|
| `message/send`, `tasks/get`, `tasks/cancel` | `SendMessage`, `GetTask`, `CancelTask` | Method-name constants in `config.py` (`A2A_METHOD_*`). |
| `kind: "message"`, `kind: "task"`, `kind: "text"`, `kind: "data"` | `kind` discriminator removed | `protocol.py` emits no `kind`; parts are `{"text": ...}` or `{"data": ...}`. |
| `role: "user"` / `"agent"` | `ROLE_USER` / `ROLE_AGENT` | `A2A_ROLE_*` mapping in `config.py`; internal DB values stay `user`/`agent`. |
| `state: "working"`, `"input-required"` | `TASK_STATE_WORKING`, `TASK_STATE_INPUT_REQUIRED` | `A2A_STATE_BY_INTERNAL` / `INTERNAL_STATE_BY_A2A` in `config.py`; DB stays short. |
| `protocolVersion` top-level in the card | Inside `supportedInterfaces[].protocolVersion`; card requires `supportedInterfaces` | Section 8.1 updated; card builder emits `supportedInterfaces`. |
| `configuration.blocking: bool` | `configuration.returnImmediately: bool` (default `false` = blocking) | Client maps `blocking = not returnImmediately`. Section 8.1 updated. |
| Task returned directly from `message/send` | `result` contains `{"task": Task}` or `{"message": Message}` | `protocol.py` response models; section 8.1 updated. |
| No version header | Clients send `A2A-Version: 1.0` | `A2A_VERSION_HEADER` / `A2A_PROTOCOL_VERSION` in `config.py`. |
| Only `-32001` / `-32002` special codes | Full `-32001`..`-32009` range | Constants in `config.py`. |

## Freeze rule

After this file is committed, the A2A spec is frozen at the hash above. If a
future implementation issue requires reading the spec again, record it as a
new pin revision in this file and in `docs/DESIGN_NOTES.md` before changing
code.
