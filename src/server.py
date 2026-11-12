"""FastAPI seller: Agent Card discovery + one JSON-RPC route (section 8.5).

Rows R0-R4 run before any lock. Rows R4b-R14 run inside the per-task lock
(or the creation lock for taskless requests), with one transaction per state
change. The turn is a background asyncio task; the HTTP request waits for it
up to BLOCKING_TIMEOUT and then returns the committed snapshot (E-4).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src import config, protocol, seller_agent, state_machine
from src.decision import Decision
from src.store import Store
from src.utility import Profile


class RpcFault(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        kind: str | None = None,
        severity: str = "error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind
        self.severity = severity


@dataclass(frozen=True)
class StartedTurn:
    """A committed R14 turn that the request may wait for outside the lock."""

    task_id: str
    turn: asyncio.Task[None]


def _fault(code: int, message: str, *, kind: str | None = None, severity: str = "error") -> RpcFault:
    return RpcFault(code, message, kind=kind, severity=severity)


def create_app(
    store: Store,
    profile: Profile,
    *,
    token: str | None = None,
    responder: Any | None = None,
    chat: Any | None = None,
    card_url: str | None = None,
    startup_recovery: bool = True,
) -> FastAPI:
    app = FastAPI(title="A2A Negotiation Seller")
    app.state.store = store
    app.state.profile = profile
    app.state.token = token
    app.state.chat = chat
    app.state.card_url = card_url or config.SELLER_URL
    app.state.turns = {}

    if responder is None:

        def default_responder(
            view: dict[str, Any],
            incoming: dict[str, Any],
            seller_profile: Profile,
            own_history: list[float],
        ) -> Decision:
            return seller_agent.respond(
                view,
                incoming,
                seller_profile,
                own_history,
                chat=app.state.chat,
            )

        app.state.responder = default_responder
    else:
        app.state.responder = responder

    if startup_recovery:
        store.startup_recovery()

    def card() -> dict[str, Any]:
        card_body: dict[str, Any] = {
            "name": "Godot Freelance Seller",
            "description": (
                "Negotiates a small Godot 2D inventory feature over a minimal "
                "A2A JSON-RPC subset."
            ),
            "version": "0.1.0",
            "supportedInterfaces": [
                {
                    "url": app.state.card_url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": config.A2A_PROTOCOL_VERSION,
                }
            ],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "negotiate_godot_job",
                    "name": "Negotiate a Godot job",
                    "description": "Counter-offers on price, delivery days and revisions.",
                    "tags": ["godot", "negotiation", "freelance"],
                }
            ],
        }
        if app.state.token:
            card_body["securitySchemes"] = {
                "bearerAuth": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
            }
        return card_body

    @app.get(config.AGENT_CARD_PATH)
    async def agent_card() -> dict[str, Any]:
        return card()

    @app.get(config.AGENT_CARD_ALIAS_PATH)
    async def agent_card_alias() -> dict[str, Any]:
        return card()

    @app.post(config.A2A_RPC_PATH)
    async def rpc(request: Request) -> JSONResponse:
        if app.state.token is not None:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {app.state.token}":
                return JSONResponse(status_code=401, content={"error": "unauthorized"})

        request_id: str | int | None = None
        raw = await request.body()

        # R0: body size.
        if len(raw) > config.MAX_MESSAGE_BYTES:
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_INVALID_REQUEST, "request too large"
                )
            )

        # R1: valid JSON.
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_PARSE_ERROR, "invalid JSON"
                )
            )

        # R2: envelope and method.
        if not isinstance(payload, dict):
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_INVALID_REQUEST, "invalid request"
                )
            )
        request_id = payload.get("id")
        try:
            envelope = protocol.JSONRPCRequest.model_validate(payload)
        except ValidationError:
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_INVALID_REQUEST, "invalid request"
                )
            )
        if envelope.method not in config.A2A_METHODS:
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_METHOD_NOT_FOUND, "method not found"
                )
            )

        try:
            if envelope.method == config.A2A_METHOD_SEND:
                result = {"task": await _send_message(envelope.params)}
            elif envelope.method == config.A2A_METHOD_GET_TASK:
                result = _get_task(envelope.params)
            else:
                result = _cancel_task(envelope.params)
        except RpcFault as fault:
            return JSONResponse(
                protocol.jsonrpc_error(request_id, fault.code, fault.message)
            )
        except Exception:
            return JSONResponse(
                protocol.jsonrpc_error(
                    request_id, config.JSONRPC_INTERNAL_ERROR, "internal error"
                )
            )

        return JSONResponse(protocol.jsonrpc_result(request_id, result))

    # -- message/send -------------------------------------------------------

    async def _send_message(params: dict[str, Any] | None) -> dict[str, Any]:
        message, offer, _blocking = _validate_send_params(params)
        task_id = message.taskId

        if not task_id:
            # Taskless replay/resume: R6 is global and must be checked before
            # minting a new task (D-8, D-16).
            stored = store.find_message(message.messageId)
            if stored is not None:
                async with store.lock_for(stored["task_id"]):
                    outcome = _handle_existing(
                        stored["task_id"], message, offer
                    )
                return await _finish(outcome)

            # R12b: an accept cannot open a new task.
            if offer["type"] == "accept":
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    "accept requires an existing task",
                    kind="bad_params",
                )

            # R4: fast pre-check before taking the creation lock (E-48).
            if store.open_task_count() >= config.MAX_OPEN_TASKS:
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["R4"],
                    kind="server_busy",
                    severity="info",
                )
            async with store.creation_lock:
                # R4b: authoritative re-check inside the lock.
                if store.open_task_count() >= config.MAX_OPEN_TASKS:
                    raise _fault(
                        config.JSONRPC_INVALID_PARAMS,
                        config.REASONS["R4"],
                        kind="server_busy",
                        severity="info",
                    )
                task = store.create_task(message.contextId)
                outcome = _begin_turn(task["id"], message, offer)
            return await _finish(outcome)

        # R5: task exists.
        if store.get_task(task_id) is None:
            raise _fault(config.A2A_TASK_NOT_FOUND, "task not found")

        async with store.lock_for(task_id):
            outcome = _handle_existing(task_id, message, offer)
        return await _finish(outcome)

    async def _finish(outcome: dict[str, Any] | StartedTurn) -> dict[str, Any]:
        if isinstance(outcome, StartedTurn):
            return await _await_turn(outcome)
        return outcome

    def _handle_existing(
        task_id: str, message: protocol.Message, offer: dict[str, Any]
    ) -> dict[str, Any] | StartedTurn:
        stored = store.find_message(message.messageId)

        if stored is not None:
            # R6a: the message belongs to another task.
            if stored["task_id"] != task_id or stored["role"] != "user":
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["R6A"],
                    kind="bad_params",
                )

            task = store.get_task(stored["task_id"])
            assert task is not None
            agent_after = any(
                row["role"] == "agent" and row["seq"] > stored["seq"]
                for row in store.messages_for_task(stored["task_id"])
            )

            # R6b: the turn already completed -> return the current task.
            if state_machine.is_terminal(task["state"]) or agent_after:
                view = store.task_view(task["id"])
                assert view is not None
                return view

            # R6d: the turn is still running for this message.
            if task["state"] == "working":
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["R6D"],
                    kind="bad_params",
                )

            # R6c: resume an incomplete turn (restart/crash).
            if task["round_at_turn_start"] is None:
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["RESUME_DRIFT"],
                    kind="bad_params",
                )
            stored_round = store.message_round(stored)
            if stored_round != task["round_at_turn_start"]:
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["RESUME_DRIFT"],
                    kind="bad_params",
                )
            store.resume_turn(task["id"])
            incoming, text = protocol.split_parts(stored["parts"])
            if incoming is None:
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["RESUME_DRIFT"],
                    kind="bad_params",
                )
            incoming = dict(incoming)
            incoming["_peer_text"] = text
            return _spawn_turn(task["id"], incoming)

        task = store.get_task(task_id)
        assert task is not None

        # R7: terminal tasks are immutable.
        if state_machine.is_terminal(task["state"]):
            raise _fault(
                config.JSONRPC_INVALID_PARAMS, config.REASONS["R7"], kind="bad_params"
            )

        # R8: a new messageId while a turn is running.
        if task["state"] == "working":
            raise _fault(
                config.JSONRPC_INVALID_PARAMS, config.REASONS["R8"], kind="bad_params"
            )

        # R9: the incoming round must match the stored offer count.
        if int(offer["round"]) != int(task["round"]):
            raise _fault(
                config.JSONRPC_INVALID_PARAMS, config.REASONS["R9"], kind="bad_round"
            )

        if offer["type"] == "accept":
            # R10: must equal the seller's last offer.
            last_agent = store.last_message(task_id, "agent")
            last_data = None
            if last_agent is not None:
                last_data, _ = protocol.split_parts(last_agent["parts"])
            if (
                last_data is None
                or last_data.get("type") != "counter"
                or last_data.get("terms") != offer["terms"]
            ):
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS,
                    config.REASONS["R10"],
                    kind="bad_params",
                )

            # R11: accepted -> completed, artifact built by code, no LLM.
            store.complete_with_accept(task_id, offer["terms"])
            view = store.task_view(task_id)
            assert view is not None
            return view

        # R13/R14: an offer on an input-required (or freshly submitted) task.
        if task["state"] in {"input-required", "submitted"}:
            return _begin_turn(task_id, message, offer)

        raise _fault(
            config.JSONRPC_INVALID_PARAMS, config.REASONS["R8"], kind="bad_params"
        )

    def _begin_turn(
        task_id: str, message: protocol.Message, offer: dict[str, Any]
    ) -> StartedTurn:
        parts = [part.model_dump(exclude_none=True) for part in message.parts]
        store.begin_turn(task_id, message.messageId, parts)
        return _spawn_turn(task_id, offer)

    def _spawn_turn(task_id: str, incoming: dict[str, Any]) -> StartedTurn:
        view = store.task_view(task_id)
        assert view is not None
        own_history = store.own_utility_history(task_id)
        turn = asyncio.create_task(
            _run_turn(task_id, incoming, view, own_history)
        )
        app.state.turns[turn] = None
        turn.add_done_callback(lambda done: app.state.turns.pop(done, None))
        return StartedTurn(task_id=task_id, turn=turn)

    async def _await_turn(started: StartedTurn) -> dict[str, Any]:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(started.turn), timeout=config.BLOCKING_TIMEOUT
            )
        current = store.task_view(started.task_id)
        assert current is not None
        return current

    async def _run_turn(
        task_id: str,
        incoming: dict[str, Any],
        view: dict[str, Any],
        own_history: list[float],
    ) -> None:
        try:
            result = app.state.responder(
                view, incoming, app.state.profile, own_history
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Decision):
                raise TypeError("responder must return a Decision")
            decision: Decision = result

            if decision.terms is None:
                raise ValueError("decision has no terms")

            if decision.kind == "counter":
                parts = [
                    protocol.data_part(
                        {
                            "type": "counter",
                            "terms": decision.terms,
                            "round": decision.round,
                        }
                    )
                ]
                if decision.rationale:
                    parts.append(protocol.text_part(decision.rationale))
                store.cas_commit(
                    task_id,
                    "input-required",
                    "counter",
                    own_u=decision.own_u,
                    agent_message_id=str(uuid.uuid4()),
                    agent_parts=parts,
                )
            elif decision.kind == "accept":
                parts = [
                    protocol.data_part(
                        {
                            "type": "accept",
                            "terms": decision.terms,
                            "round": decision.round,
                        }
                    )
                ]
                store.cas_commit(
                    task_id,
                    "completed",
                    "accept",
                    own_u=decision.own_u,
                    agent_message_id=str(uuid.uuid4()),
                    agent_parts=parts,
                    agreement_terms=decision.terms,
                )
            elif decision.kind == "walk_away":
                parts = [
                    protocol.data_part(
                        {
                            "type": "walk_away",
                            "terms": decision.terms,
                            "round": decision.round,
                        }
                    )
                ]
                if decision.rationale:
                    parts.append(protocol.text_part(decision.rationale))
                store.cas_commit(
                    task_id,
                    "rejected",
                    "walk_away",
                    agent_message_id=str(uuid.uuid4()),
                    agent_parts=parts,
                )
            else:
                raise ValueError(f"unknown decision kind {decision.kind!r}")
        except asyncio.CancelledError:
            raise
        except Exception:
            # E-22: a turn never leaves a task stuck in `working`.
            store.cas_commit(task_id, "failed", "failed")

    # -- tasks/get and tasks/cancel ----------------------------------------

    def _get_task(params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
        history_length = params.get("historyLength")
        if history_length is not None:
            if (
                not isinstance(history_length, int)
                or isinstance(history_length, bool)
                or history_length < 0
            ):
                raise _fault(
                    config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params"
                )
        else:
            history_length = config.MAX_HISTORY_RESPONSE

        view = store.task_view(task_id, history_length)
        if view is None:
            raise _fault(config.A2A_TASK_NOT_FOUND, "task not found")
        return view

    def _cancel_task(params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

        _task, error = store.cancel_task(task_id)
        if error == config.A2A_TASK_NOT_FOUND:
            raise _fault(config.A2A_TASK_NOT_FOUND, "task not found")
        if error == config.A2A_TASK_NOT_CANCELABLE:
            raise _fault(config.A2A_TASK_NOT_CANCELABLE, "task not cancelable")
        view = store.task_view(task_id)
        assert view is not None
        return view

    return app


def _validate_send_params(
    params: dict[str, Any] | None,
) -> tuple[protocol.Message, dict[str, Any], bool]:
    """R3: schema and strict types, before any lock."""

    if not isinstance(params, dict):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    raw_message = params.get("message")
    if not isinstance(raw_message, dict):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

    try:
        message = protocol.Message.model_validate(raw_message)
    except ValidationError:
        raise _fault(
            config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params"
        ) from None

    if message.internal_role != "user":
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

    if any(part.raw is not None or part.url is not None for part in message.parts):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

    data_parts = [part for part in message.parts if part.data is not None]
    text_parts = [part for part in message.parts if part.text is not None]
    if len(data_parts) != 1 or len(text_parts) > 1:
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    if text_parts and len(text_parts[0].text or "") > config.MAX_RATIONALE_CHARS:
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    if not isinstance(data_parts[0].data, dict):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

    try:
        offer = protocol.OfferData.model_validate(data_parts[0].data)
    except ValidationError:
        raise _fault(
            config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params"
        ) from None
    if offer.type not in {"offer", "accept"}:
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")

    configuration = params.get("configuration")
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    return_immediately = configuration.get("returnImmediately", False)
    legacy_blocking = configuration.get("blocking")
    if not isinstance(return_immediately, bool):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    if legacy_blocking is not None and not isinstance(legacy_blocking, bool):
        raise _fault(config.JSONRPC_INVALID_PARAMS, "invalid params", kind="bad_params")
    blocking = legacy_blocking if legacy_blocking is not None else not return_immediately

    return message, offer.as_dict(), blocking
