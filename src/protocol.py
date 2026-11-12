"""A2A 1.0 wire models (pinned in docs/A2A_PIN.md).

Wire shapes follow A2A 1.0: no ``kind`` discriminator, ``ROLE_*`` roles and
``TASK_STATE_*`` states. Internal database/state-machine names stay short and
are mapped in :mod:`src.config`.

Terms are strict ints on the wire (E-7): floats and bools are rejected with
``-32602`` and are never coerced, keeping the agreement hash stable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator

from src import config

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class Terms(BaseModel):
    """The three negotiable issues. Strict ints only."""

    model_config = ConfigDict(extra="ignore")

    price: StrictInt
    delivery_days: StrictInt
    revisions: StrictInt

    def as_dict(self) -> dict[str, int]:
        return {
            "price": int(self.price),
            "delivery_days": int(self.delivery_days),
            "revisions": int(self.revisions),
        }


OfferType = Literal["offer", "counter", "accept", "walk_away"]


class OfferData(BaseModel):
    """The structured payload carried in a single ``data`` part."""

    model_config = ConfigDict(extra="ignore")

    type: OfferType
    terms: Terms
    round: StrictInt

    @field_validator("round")
    @classmethod
    def _round_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("round must be >= 0")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "terms": self.terms.as_dict(),
            "round": int(self.round),
        }


class Part(BaseModel):
    """A2A 1.0 Part: exactly one of text/raw/url/data."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: Any | None = None
    metadata: dict[str, Any] | None = None
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def _exactly_one_content(self) -> Part:
        filled = sum(
            value is not None for value in (self.text, self.raw, self.url, self.data)
        )
        if filled != 1:
            raise ValueError("a Part MUST contain exactly one of text/raw/url/data")
        return self


class Message(BaseModel):
    """A2A 1.0 Message. ``kind`` was removed in 1.0 and is ignored if sent."""

    model_config = ConfigDict(extra="ignore")

    messageId: str
    role: str
    parts: list[Part]
    taskId: str | None = None
    contextId: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in {
            "ROLE_USER",
            "ROLE_AGENT",
            "user",
            "agent",
        }:
            raise ValueError("unknown role")
        return value

    @field_validator("messageId")
    @classmethod
    def _message_id_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("messageId must be non-empty")
        return value

    @property
    def internal_role(self) -> str:
        return "user" if self.role in {"ROLE_USER", "user"} else "agent"

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class TaskStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str
    message: Message | None = None
    timestamp: str | None = None


class Artifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    artifactId: str
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    contextId: str | None = None
    status: TaskStatus
    history: list[Message] | None = None
    artifacts: list[Artifact] | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Agent discovery
# ---------------------------------------------------------------------------


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    streaming: bool = False
    pushNotifications: bool = False


class AgentSkill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str
    tags: list[str]


class AgentInterface(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    protocolBinding: str
    protocolVersion: str


class AgentCard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    supportedInterfaces: list[AgentInterface]
    version: str
    capabilities: AgentCapabilities
    defaultInputModes: list[str]
    defaultOutputModes: list[str]
    skills: list[AgentSkill]
    securitySchemes: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelopes
# ---------------------------------------------------------------------------


class JSONRPCRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


class JSONRPCErrorObject(BaseModel):
    code: int
    message: str
    data: list[Any] | None = None


class JSONRPCResponse(BaseModel):
    """Only used for building responses; the server returns plain dicts."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: JSONRPCErrorObject | None = None


def jsonrpc_result(request_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    data: list[Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def error_data(kind: str, detail: str | None = None) -> list[dict[str, Any]]:
    """Minimal ProtoJSON-Any-ish detail object used in error.data."""

    item: dict[str, Any] = {"@type": f"type.googleapis.com/a2a.demo.{kind}"}
    if detail is not None:
        item["detail"] = detail
    return [item]


# ---------------------------------------------------------------------------
# Part helpers
# ---------------------------------------------------------------------------


def text_part(text: str) -> dict[str, Any]:
    return {"text": text}


def data_part(data: dict[str, Any]) -> dict[str, Any]:
    return {"data": data}


def split_parts(parts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (data payload, text) from raw wire parts.

    R3 has already guaranteed one data part and at most one text part.
    """

    data: dict[str, Any] | None = None
    text: str | None = None
    for part in parts:
        if "data" in part and part["data"] is not None:
            data = part["data"]
        elif "text" in part and part["text"] is not None:
            text = part["text"]
    return data, text


# ---------------------------------------------------------------------------
# Agreement integrity (D-14)
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def agreement_hash(task_id: str, terms: dict[str, int]) -> str:
    payload = {"task_id": task_id, "terms": terms}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def agreement_artifact(task_id: str, terms: dict[str, int]) -> dict[str, Any]:
    return {
        "artifactId": f"agreement-{task_id}",
        "name": "agreement",
        "parts": [
            data_part(
                {
                    "type": "agreement",
                    "task_id": task_id,
                    "terms": terms,
                    "sha256": agreement_hash(task_id, terms),
                }
            )
        ],
    }


def verify_agreement(task_id: str, terms: dict[str, int], digest: str) -> bool:
    return agreement_hash(task_id, terms) == digest


# Re-exported for convenience in server code.
MAX_MESSAGE_BYTES = config.MAX_MESSAGE_BYTES
