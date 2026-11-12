"""Buyer CLI: drive one negotiation against a seller URL (section 8.7)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from src import config, llm, protocol, trace
from src.buyer_agent import next_move
from src.buyer_store import BuyerStore
from src.client import A2AClient, A2AClientError, SendFailed
from src.decision import Decision
from src.utility import Profile, load_profile


class AgreementMismatch(Exception):
    pass


def _message_for(decision: Decision, task_id: str | None, context_id: str) -> dict[str, Any]:
    assert decision.terms is not None
    data = {
        "type": "offer" if decision.kind == "offer" else "accept",
        "terms": decision.terms,
        "round": decision.round,
    }
    parts: list[dict[str, Any]] = [protocol.data_part(data)]
    if decision.rationale:
        parts.append(protocol.text_part(decision.rationale))
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": parts,
        "contextId": context_id,
    }
    if task_id:
        message["taskId"] = task_id
    return message


def verify_completed(view: dict[str, Any], profile: Profile) -> dict[str, Any]:
    task_id = view["id"]
    artifacts = view.get("artifacts") or []
    for artifact in artifacts:
        if artifact.get("name") != "agreement":
            continue
        data = None
        for part in artifact.get("parts") or []:
            if isinstance(part.get("data"), dict):
                data = part["data"]
        if data is None:
            continue
        terms = data.get("terms")
        digest = data.get("sha256")
        if (
            isinstance(terms, dict)
            and isinstance(digest, str)
            and protocol.verify_agreement(task_id, terms, digest)
        ):
            return {
                "outcome": "completed",
                "task_id": task_id,
                "terms": terms,
                "own_utility": profile.utility(terms),
            }
    raise AgreementMismatch("artifact hash does not verify")


def _as_agent_kind(message: dict[str, Any]) -> str:
    for part in message.get("parts") or []:
        data = part.get("data")
        if isinstance(data, dict):
            data_type = data.get("type")
            if data_type in {"counter", "accept", "walk_away"}:
                return str(data_type)
    return "counter"


def _data_of(message: dict[str, Any]) -> dict[str, Any]:
    for part in message.get("parts") or []:
        data = part.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _log_view(
    view: dict[str, Any],
    agent_name: str,
    logged: set[str],
) -> None:
    """Trace any not-yet-traced agent messages in this view (in direction)."""
    task_id = view.get("id")
    for message in view.get("history") or []:
        role = message.get("role")
        if role not in {"ROLE_AGENT", "agent"}:
            continue
        message_id = message.get("messageId")
        if not message_id or message_id in logged:
            continue
        logged.add(message_id)
        data = _data_of(message)
        round_value = data.get("round")
        trace.trace(
            _as_agent_kind(message),
            direction="in",
            agent=agent_name,
            task_id=task_id,
            round=round_value if isinstance(round_value, int) else None,
            terms=data.get("terms") if isinstance(data.get("terms"), dict) else None,
        )


async def negotiate(
    client: A2AClient,
    profile: Profile,
    *,
    verbose: bool = True,
    max_steps: int = 100,
    initial_view: dict[str, Any] | None = None,
    buyer_store: BuyerStore | None = None,
    url: str | None = None,
    chat: Any | None = None,
) -> dict[str, Any]:
    agent_name = profile.name
    await client.fetch_card()
    context_id = str(uuid.uuid4())
    task_id: str | None = None
    logged: set[str] = set()
    view: dict[str, Any] = initial_view or {
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "history": [],
        "artifacts": [],
    }

    if initial_view is not None and initial_view.get("id"):
        task_id = initial_view["id"]
        _log_view(initial_view, agent_name, logged)

    def _remember(message: dict[str, Any], returned_view: dict[str, Any]) -> None:
        if buyer_store is not None and url is not None:
            buyer_store.record(url, returned_view["id"], message)

    def _forget(final_task_id: str | None) -> None:
        if buyer_store is not None and url is not None and final_task_id is not None:
            buyer_store.clear(url, final_task_id)

    for _ in range(max_steps):
        decision = next_move(view, profile, chat=chat)

        if decision.kind in {"complete"}:
            result = verify_completed(view, profile)
            _forget(task_id)
            if verbose:
                print(f"AGREED {result['terms']} U_buyer={result['own_utility']:.2f}")
            return result
        if decision.kind in {"rejected", "canceled", "failed", "protocol_error"}:
            outcome = "canceled" if decision.kind == "cancel" else decision.kind
            _forget(task_id)
            if verbose:
                print(f"END outcome={outcome} reason={decision.reason or ''}")
            return {"outcome": outcome, "reason": decision.reason, "task_id": task_id}

        if decision.kind == "wait":
            if task_id is None:
                raise SendFailed("nothing to wait for")
            view = await client.poll_task(task_id)
            _log_view(view, agent_name, logged)
            continue

        if decision.kind == "cancel":
            if task_id is not None:
                view = await client.cancel_task(task_id)
            _forget(task_id)
            if verbose:
                print("END outcome=canceled reason=max_rounds")
            return {"outcome": "canceled", "reason": decision.reason, "task_id": task_id}

        if decision.kind == "resend":
            if decision.message is None:
                raise SendFailed("resend decision without a message")
            message = decision.message
        else:
            message = _message_for(decision, task_id, context_id)

        view = await client.send_message(message)
        task_id = view["id"]
        if decision.kind in {"offer", "accept"} and decision.terms is not None:
            trace.trace(
                decision.kind,
                direction="out",
                agent=agent_name,
                task_id=task_id,
                round=decision.round,
                terms=decision.terms,
                own_u=decision.own_u,
                source=decision.source,
                tokens_in=decision.tokens_in,
                tokens_out=decision.tokens_out,
            )
            _remember(message, view)
        _log_view(view, agent_name, logged)
        if verbose:
            terms = decision.terms if decision.terms is not None else {}
            own_u = f"{decision.own_u:.2f}" if decision.own_u is not None else "-"
            print(
                f"round={decision.round} kind={decision.kind} terms={terms} "
                f"own_U={own_u} state={view['status']['state']}"
            )

    _forget(task_id)
    raise SendFailed("negotiation did not terminate")


async def main_async(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    token = args.token
    client = A2AClient(args.seller, token=token)
    buyer_store = BuyerStore(config.BUYER_DB_PATH, journal_mode="WAL")
    try:
        try:
            if args.resume:
                view = await _resume(client, args.seller, buyer_store, args.verbose)
                if view is None:
                    return 1
                await negotiate(
                    client,
                    profile,
                    verbose=args.verbose,
                    initial_view=view,
                    buyer_store=buyer_store,
                    url=args.seller,
                    chat=llm.default_chat(),
                )
            else:
                await negotiate(
                    client,
                    profile,
                    verbose=args.verbose,
                    buyer_store=buyer_store,
                    url=args.seller,
                    chat=llm.default_chat(),
                )
        except AgreementMismatch as exc:
            print(f"AGREEMENT_MISMATCH: {exc}", file=sys.stderr)
            return 2
        except (A2AClientError, SendFailed) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    finally:
        await client.close()
        buyer_store.close()
    return 0


async def _resume(
    client: A2AClient,
    url: str,
    buyer_store: BuyerStore,
    verbose: bool,
) -> dict[str, Any] | None:
    """Load the stored last message; resume byte-identically when needed."""
    row = buyer_store.latest(url)
    if row is None:
        print("ERROR: --resume but no stored message for this seller", file=sys.stderr)
        return None
    task_id = row["task_id"]
    try:
        view = await client.get_task(task_id)
    except (A2AClientError, SendFailed) as exc:
        print(f"ERROR: could not load task {task_id}: {exc}", file=sys.stderr)
        return None

    state = str(view.get("status", {}).get("state", ""))
    history = view.get("history") or []
    last = history[-1] if history else None
    last_role = last.get("role") if last else None
    incomplete = last is not None and last_role in {"ROLE_USER", "user"}

    terminal_states = {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_FAILED",
    }
    if state in terminal_states or not incomplete:
        # The turn finished before we crashed; continue from the snapshot.
        return view

    # The stored turn is incomplete: resend the exact same bytes (E-33).
    message = json.loads(row["last_message_json"])
    if verbose:
        print(f"RESUME task={task_id} message={message.get('messageId')}")
    view = await client.send_message(message)
    return view


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buyer_cli")
    parser.add_argument("--seller", default=config.SELLER_URL)
    parser.add_argument("--profile", default="profiles/buyer.json")
    parser.add_argument("--token", default=config.A2A_TOKEN)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args(argv)
    trace.configure(config.TRACE_PATH)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
