"""A2A JSON-RPC client (section 8.8)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src import config


class A2AClientError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class IncompatibleAgent(Exception):
    pass


class AuthError(Exception):
    pass


class SendFailed(Exception):
    pass


Sleep = Callable[[float], Awaitable[None]]


class A2AClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = config.HTTP_TIMEOUT,
        *,
        sleep: Sleep = asyncio.sleep,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._sleep = sleep
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout
        )

    async def __aenter__(self) -> A2AClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            config.A2A_VERSION_HEADER: config.A2A_PROTOCOL_VERSION,
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def fetch_card(self) -> dict[str, Any]:
        response = await self._client.get(config.AGENT_CARD_PATH)
        if response.status_code != 200:
            raise SendFailed(f"agent card returned HTTP {response.status_code}")
        card = response.json()
        for field in ("name", "description", "version", "skills", "supportedInterfaces"):
            if field not in card:
                raise IncompatibleAgent(f"agent card missing {field}")
        interfaces = card.get("supportedInterfaces") or []
        versions = [
            interface.get("protocolVersion")
            for interface in interfaces
            if interface.get("protocolBinding") == "JSONRPC"
        ]
        if not versions or config.A2A_PROTOCOL_VERSION not in versions:
            raise IncompatibleAgent(
                f"unsupported protocol version(s): {versions!r}"
            )
        return card

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        retry: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": method,
            "params": params or {},
        }
        started = time.monotonic()
        delays = [1.0, 2.0, 4.0]
        attempt = 0

        while True:
            try:
                response = await self._client.post(
                    config.A2A_RPC_PATH, json=payload, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.TransportError):
                response = None

            if response is not None and response.status_code == 401:
                raise AuthError("unauthorized")
            if response is not None and response.status_code < 500:
                if response.status_code != 200:
                    raise SendFailed(f"HTTP {response.status_code}")
                body = response.json()
                if "error" in body:
                    error = body["error"]
                    raise A2AClientError(int(error["code"]), str(error["message"]))
                return body.get("result") or {}

            if not retry or attempt >= len(delays):
                raise SendFailed("request failed after retries")
            if time.monotonic() - started + delays[attempt] > config.MAX_RETRY_TOTAL:
                raise SendFailed("retry budget exhausted")
            await self._sleep(delays[attempt])
            attempt += 1

    async def send_message(
        self, message: dict[str, Any], *, wait: bool = True
    ) -> dict[str, Any]:
        params = {
            "message": message,
            "configuration": {"returnImmediately": not wait},
        }
        result = await self.rpc(config.A2A_METHOD_SEND, params, retry=True)
        if "task" in result:
            task = result["task"]
        elif "message" in result:
            raise SendFailed("agent returned a direct Message instead of a Task")
        else:
            raise SendFailed("malformed SendMessage result")

        if wait and task.get("status", {}).get("state") == "TASK_STATE_WORKING":
            task = await self.poll_task(task["id"])
        return task

    async def get_task(
        self, task_id: str, *, history_length: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return await self.rpc(config.A2A_METHOD_GET_TASK, params)

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        return await self.rpc(config.A2A_METHOD_CANCEL_TASK, {"id": task_id})

    async def poll_task(
        self,
        task_id: str,
        *,
        interval: float = config.POLL_INTERVAL,
        max_wait: float = config.POLL_MAX,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            task = await self.get_task(task_id)
            if task.get("status", {}).get("state") != "TASK_STATE_WORKING":
                return task
            await self._sleep(interval)
        raise SendFailed("poll timed out")
