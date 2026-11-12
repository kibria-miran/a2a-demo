"""Buyer CLI: drive one negotiation against a seller URL (section 8.7)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

from src import config, protocol
from src.buyer_agent import next_move
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
    raise AgreementMismatch("completed task has no verified agreement artifact")


async def negotiate(
    client: A2AClient,
    profile: Profile,
    *,
    verbose: bool = True,
    max_steps: int = 100,
) -> dict[str, Any]:
    await client.fetch_card()
    context_id = str(uuid.uuid4())
    task_id: str | None = None
    view: dict[str, Any] = {
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "history": [],
        "artifacts": [],
    }

    for _ in range(max_steps):
        decision = next_move(view, profile)

        if decision.kind in {"complete"}:
            result = verify_completed(view, profile)
            if verbose:
                print(f"AGREED {result['terms']} U_buyer={result['own_utility']:.2f}")
            return result
        if decision.kind in {"rejected", "canceled", "failed", "protocol_error"}:
            outcome = "canceled" if decision.kind == "cancel" else decision.kind
            if verbose:
                print(f"END outcome={outcome} reason={decision.reason or ''}")
            return {"outcome": outcome, "reason": decision.reason, "task_id": task_id}

        if decision.kind == "wait":
            if task_id is None:
                raise SendFailed("nothing to wait for")
            view = await client.poll_task(task_id)
            continue

        if decision.kind == "cancel":
            if task_id is not None:
                view = await client.cancel_task(task_id)
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
        if verbose:
            terms = decision.terms if decision.terms is not None else {}
            own_u = f"{decision.own_u:.2f}" if decision.own_u is not None else "-"
            print(
                f"round={decision.round} kind={decision.kind} terms={terms} "
                f"own_U={own_u} state={view['status']['state']}"
            )

    raise SendFailed("negotiation did not terminate")


async def main_async(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    token = args.token
    client = A2AClient(args.seller, token=token)
    try:
        try:
            await negotiate(client, profile)
        except AgreementMismatch as exc:
            print(f"AGREEMENT_MISMATCH: {exc}", file=sys.stderr)
            return 2
        except (A2AClientError, SendFailed) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    finally:
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buyer_cli")
    parser.add_argument("--seller", default=config.SELLER_URL)
    parser.add_argument("--profile", default="profiles/buyer.json")
    parser.add_argument("--token", default=config.A2A_TOKEN)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.resume:
        print("--resume is wired in Phase 4", file=sys.stderr)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
