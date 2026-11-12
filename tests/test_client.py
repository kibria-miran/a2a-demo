"""A2AClient tests over httpx.MockTransport: headers, retries, errors."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src import config
from src.client import A2AClient, A2AClientError, AuthError, IncompatibleAgent, SendFailed

pytestmark = pytest.mark.fast


def card(version: str = "1.0", binding: str = "JSONRPC") -> dict[str, Any]:
    return {
        "name": "Seller",
        "description": "Seller",
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": "http://seller",
                "protocolBinding": binding,
                "protocolVersion": version,
            }
        ],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{"id": "s", "name": "s", "description": "s", "tags": []}],
    }


def task(state: str = "TASK_STATE_COMPLETED", task_id: str = "t1") -> dict[str, Any]:
    return {
        "id": task_id,
        "status": {"state": state},
        "history": [],
        "artifacts": [],
    }


def make_client(handler, **kwargs: Any) -> tuple[A2AClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    raw = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return A2AClient("http://testserver", http_client=raw, **kwargs), raw


async def test_fetch_card_accepts_pinned_version() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == config.AGENT_CARD_PATH
        return httpx.Response(200, json=card())

    client, raw = make_client(handler)
    try:
        assert (await client.fetch_card())["name"] == "Seller"
    finally:
        await raw.aclose()


@pytest.mark.parametrize(
    ("version", "binding"),
    [("0.3", "JSONRPC"), ("1.0", "GRPC")],
)
async def test_fetch_card_rejects_incompatible_agents(version: str, binding: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card(version, binding))

    client, raw = make_client(handler)
    try:
        with pytest.raises(IncompatibleAgent):
            await client.fetch_card()
    finally:
        await raw.aclose()


async def test_send_message_sends_version_and_bearer_headers() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {"task": task()}}
        )

    client, raw = make_client(handler, token="secret")
    try:
        message = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
        result = await client.send_message(message)
    finally:
        await raw.aclose()

    assert result["id"] == "t1"
    assert seen["headers"][config.A2A_VERSION_HEADER.lower()] == "1.0"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["body"]["method"] == config.A2A_METHOD_SEND
    assert seen["body"]["params"]["message"] == message
    assert seen["body"]["params"]["configuration"]["returnImmediately"] is False


async def test_wait_false_does_not_poll() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        assert body["params"]["configuration"]["returnImmediately"] is True
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"task": task("TASK_STATE_WORKING")},
            },
        )

    client, raw = make_client(handler)
    try:
        result = await client.send_message(
            {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]},
            wait=False,
        )
    finally:
        await raw.aclose()
    assert result["status"]["state"] == "TASK_STATE_WORKING"
    assert methods == [config.A2A_METHOD_SEND]


async def test_waits_by_polling_get_task() -> None:
    methods: list[str] = []
    gets = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] == config.A2A_METHOD_SEND:
            payload = {"task": task("TASK_STATE_WORKING")}
        else:
            gets["n"] += 1
            state = "TASK_STATE_WORKING" if gets["n"] == 1 else "TASK_STATE_COMPLETED"
            payload = task(state)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": payload}
        )

    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    client, raw = make_client(handler, sleep=sleep)
    try:
        result = await client.send_message(
            {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
        )
    finally:
        await raw.aclose()
    assert result["status"]["state"] == "TASK_STATE_COMPLETED"
    assert methods == [
        config.A2A_METHOD_SEND,
        config.A2A_METHOD_GET_TASK,
        config.A2A_METHOD_GET_TASK,
    ]
    assert slept == [config.POLL_INTERVAL]


async def test_jsonrpc_error_is_raised() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "error": {"code": -32602, "message": "invalid params"},
            },
        )

    client, raw = make_client(handler)
    try:
        with pytest.raises(A2AClientError) as info:
            await client.get_task("t1")
    finally:
        await raw.aclose()
    assert info.value.code == -32602


async def test_401_raises_auth_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client, raw = make_client(handler)
    try:
        with pytest.raises(AuthError):
            await client.get_task("t1")
    finally:
        await raw.aclose()


async def test_retry_reuses_the_same_body_and_backs_off() -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {"task": task()}}
        )

    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    client, raw = make_client(handler, sleep=sleep)
    try:
        await client.send_message(
            {"messageId": "same-id", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
        )
    finally:
        await raw.aclose()

    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert bodies[0]["params"]["message"]["messageId"] == "same-id"
    assert slept == [1.0]


async def test_retry_budget_exhaustion_raises_send_failed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async def sleep(_delay: float) -> None:
        return None

    client, raw = make_client(handler, sleep=sleep)
    try:
        with pytest.raises(SendFailed):
            await client.send_message(
                {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
            )
    finally:
        await raw.aclose()
