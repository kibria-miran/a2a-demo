"""Wire-model tests: strict ints, extra fields, A2A 1.0 shape (E-7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src import protocol

pytestmark = pytest.mark.fast


def test_terms_accept_strict_ints() -> None:
    terms = protocol.Terms(price=800, delivery_days=14, revisions=3)
    assert terms.as_dict() == {"price": 800, "delivery_days": 14, "revisions": 3}


@pytest.mark.parametrize(
    "bad",
    [
        {"price": 800.0, "delivery_days": 14, "revisions": 3},
        {"price": 800, "delivery_days": 14.5, "revisions": 3},
        {"price": 800, "delivery_days": 14, "revisions": 3.0},
        {"price": True, "delivery_days": 14, "revisions": 3},
        {"price": 800, "delivery_days": False, "revisions": 3},
        {"price": "800", "delivery_days": 14, "revisions": 3},
    ],
)
def test_terms_reject_floats_bools_and_strings(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        protocol.Terms(**bad)  # type: ignore[arg-type]


def test_offer_data_round_is_strict_int() -> None:
    assert protocol.OfferData(type="offer", terms=protocol.Terms(price=1, delivery_days=2, revisions=3), round=0)
    with pytest.raises(ValidationError):
        protocol.OfferData(type="offer", terms=protocol.Terms(price=1, delivery_days=2, revisions=3), round="0")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        protocol.OfferData(type="offer", terms=protocol.Terms(price=1, delivery_days=2, revisions=3), round=-1)


def test_part_requires_exactly_one_content() -> None:
    assert protocol.Part(text="hi").text == "hi"
    assert protocol.Part(data={"x": 1}).data == {"x": 1}
    with pytest.raises(ValidationError):
        protocol.Part()
    with pytest.raises(ValidationError):
        protocol.Part(text="hi", data={"x": 1})


def test_message_ignores_legacy_kind_and_unknown_fields() -> None:
    msg = protocol.Message.model_validate(
        {
            "kind": "message",
            "messageId": "m1",
            "role": "user",
            "parts": [{"kind": "data", "data": {"x": 1}}],
            "unknown": True,
        }
    )
    assert msg.internal_role == "user"
    assert msg.to_wire() == {
        "messageId": "m1",
        "role": "user",
        "parts": [{"data": {"x": 1}}],
    }


def test_message_rejects_unknown_role_and_empty_id() -> None:
    with pytest.raises(ValidationError):
        protocol.Message(messageId="m", role="system", parts=[{"text": "x"}])
    with pytest.raises(ValidationError):
        protocol.Message(messageId="", role="user", parts=[{"text": "x"}])


def test_task_and_artifact_models() -> None:
    task = protocol.Task(
        id="t1",
        status=protocol.TaskStatus(state="TASK_STATE_WORKING"),
        history=[],
        artifacts=[],
    )
    assert task.status.state == "TASK_STATE_WORKING"

    artifact = protocol.Artifact.model_validate(
        protocol.agreement_artifact("t1", {"price": 900, "delivery_days": 14, "revisions": 3})
    )
    assert artifact.name == "agreement"
    assert artifact.parts[0].data["type"] == "agreement"


def test_agent_card_requires_supported_interfaces() -> None:
    card = protocol.AgentCard(
        name="Seller",
        description="Godot freelancer",
        version="0.1.0",
        supportedInterfaces=[
            protocol.AgentInterface(
                url="http://127.0.0.1:8002",
                protocolBinding="JSONRPC",
                protocolVersion="1.0",
            )
        ],
        capabilities=protocol.AgentCapabilities(),
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
        skills=[
            protocol.AgentSkill(
                id="negotiate_godot_job",
                name="Negotiate a Godot job",
                description="Counter-offers on price, days and revisions.",
                tags=["godot", "negotiation"],
            )
        ],
    )
    wire = card.model_dump(exclude_none=True)
    assert wire["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert "protocolVersion" not in wire  # v1.0 moved it onto each interface


def test_jsonrpc_request_model() -> None:
    req = protocol.JSONRPCRequest.model_validate(
        {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}
    )
    assert req.method == "SendMessage"
    with pytest.raises(ValidationError):
        protocol.JSONRPCRequest.model_validate({"jsonrpc": "1.0", "method": "x"})


def test_agreement_hash_is_key_order_independent_and_stable() -> None:
    a = {"price": 900, "delivery_days": 14, "revisions": 3}
    b = {"revisions": 3, "price": 900, "delivery_days": 14}
    assert protocol.agreement_hash("t1", a) == protocol.agreement_hash("t1", b)
    assert protocol.agreement_hash("t1", a) != protocol.agreement_hash("t2", a)
    assert protocol.verify_agreement("t1", a, protocol.agreement_hash("t1", a))
    assert not protocol.verify_agreement("t1", a, "0" * 64)


def test_jsonrpc_error_has_code_message_and_optional_data() -> None:
    err = protocol.jsonrpc_error(7, -32602, "Invalid params", protocol.error_data("BadRequest"))
    assert err["id"] == 7
    assert err["error"]["code"] == -32602
    assert err["error"]["data"][0]["@type"].endswith("BadRequest")
