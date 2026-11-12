"""Fake NIM chat API for the slow tier (E-37, E-52).

A real HTTP subprocess (NOT a monkeypatch — patching does not cross the
process boundary). Serves a scripted sequence of chat completions so a
real uvicorn seller can run its actual LLM path with zero NVIDIA calls.

Protocol (all state per-instance):
  POST /__stage    {"responses": ["...", ...]}   append scripted contents
  POST /__control  {"sleep_call": N, "sleep_s": S}  sleep S seconds before
                    serving call N (1-indexed); or
                    {"sleep_calls": {"1": 8.0, "2": 8.0}} for several calls
  POST /__reset    drop all queued responses, calls and sleep config
  GET  /__state    {"calls": int, "remaining": int}
  POST /chat/completions      (and /v1/chat/completions) — OpenAI-compatible

Prints its bound port on the first stdout line; the fixture reads it with
subprocess.Popen(...).stdout.readline().
"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

state: dict[str, object] = {
    "queue": [],
    "calls": 0,
    "sleep_calls": {},
}

app = FastAPI(title="fake-nim")


def _pop_response() -> str:
    queue = state["queue"]
    assert isinstance(queue, list)
    if queue:
        return str(queue.pop(0))
    # Nothing staged: yield an invalid proposal so the D-13 retry applies.
    return "{}"


def _completion_body(content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


@app.post("/__stage")
async def stage(request: Request) -> JSONResponse:
    payload = await request.json()
    responses = payload.get("responses", [])
    assert isinstance(responses, list)
    queue = state["queue"]
    assert isinstance(queue, list)
    queue.extend(str(item) for item in responses)
    return JSONResponse({"remaining": len(queue)})


@app.post("/__control")
async def control(request: Request) -> JSONResponse:
    payload = await request.json()
    sleep_calls = state["sleep_calls"]
    assert isinstance(sleep_calls, dict)
    if "sleep_calls" in payload:
        for call, seconds in payload["sleep_calls"].items():
            sleep_calls[int(call)] = float(seconds)
    if "sleep_call" in payload:
        sleep_calls[int(payload["sleep_call"])] = float(payload["sleep_s"])
    return JSONResponse({"sleep_calls": {str(k): v for k, v in sleep_calls.items()}})


@app.post("/__reset")
async def reset() -> JSONResponse:
    state["queue"] = []
    state["calls"] = 0
    state["sleep_calls"] = {}
    return JSONResponse({"ok": True})


@app.get("/__state")
async def state_info() -> JSONResponse:
    queue = state["queue"]
    assert isinstance(queue, list)
    return JSONResponse({"calls": state["calls"], "remaining": len(queue)})


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    state["calls"] = int(state["calls"]) + 1
    call_no = state["calls"]
    sleep_calls = state["sleep_calls"]
    assert isinstance(sleep_calls, dict)
    delay = sleep_calls.get(call_no)
    if delay:
        # asyncio.sleep keeps the fake's event loop responsive.
        await asyncio.sleep(delay)
    content = _pop_response()
    return JSONResponse(_completion_body(content))


def main() -> None:
    # Bind to an ephemeral port ourselves so the child can print it first.
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    sock.close()
    print(port, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()
