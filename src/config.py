"""Frozen configuration for the A2A negotiation demo.

Everything in this module is a constant. Nothing here performs I/O except
loading ``.env`` (which only reads environment variables). Private profile
values never live here; see ``profiles/*.json``.

The A2A wire vocabulary is pinned in ``docs/A2A_PIN.md``. Internal names
(used by the database, state machine and tests) stay short; the maps at the
bottom translate them to A2A 1.0 wire values.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Ports / addresses
# ---------------------------------------------------------------------------

SELLER_HOST = "127.0.0.1"
SELLER_PORT = 8002
SELLER_URL = f"http://{SELLER_HOST}:{SELLER_PORT}"

BUYER_HOST = "127.0.0.1"  # the buyer is a CLI; kept for symmetry and evals

# ---------------------------------------------------------------------------
# Caps and limits
# ---------------------------------------------------------------------------

MAX_ROUNDS = 8  # T
MAX_MESSAGE_BYTES = 16_384
MAX_OPEN_TASKS = 20
MAX_HISTORY_RESPONSE = 20
MAX_RATIONALE_CHARS = 400

# ---------------------------------------------------------------------------
# Negotiation curve and concession band
# ---------------------------------------------------------------------------

START_U = 0.90
END_U = 0.10
BETA_DEFAULT = 2.0
BAND_LOW = 0.05
BAND_HIGH = 0.10

# ---------------------------------------------------------------------------
# Tolerances (never loosen these to hide a bug)
# ---------------------------------------------------------------------------

EPS_ROUNDING = 0.03
EPS_MONOTONE = 0.02
EPS_BAND = 0.03

# ---------------------------------------------------------------------------
# Timeouts, retries and polling (seconds unless stated otherwise)
# ---------------------------------------------------------------------------

LLM_TIMEOUT = 30.0
BLOCKING_TIMEOUT = 45.0
HTTP_TIMEOUT = 60.0
MAX_RETRY_TOTAL = 15.0
POLL_MAX = 60.0
POLL_INTERVAL = 2.0

LLM_TEMPERATURE = 0.3
LLM_RETRY_ATTEMPTS = 1  # one retry after the first failure (D-13)

# ---------------------------------------------------------------------------
# Storage / tracing
# ---------------------------------------------------------------------------

SQLITE_BUSY_TIMEOUT_MS = 5_000
TRACE_U_DECIMALS = 2
PRICE_STEP = 10  # lattice-search granularity for the price issue

# ---------------------------------------------------------------------------
# A2A protocol pin (see docs/A2A_PIN.md)
# ---------------------------------------------------------------------------

A2A_SPEC_VERSION = "1.0.0"
A2A_PROTOCOL_VERSION = "1.0"
A2A_VERSION_HEADER = "A2A-Version"

AGENT_CARD_PATH = "/.well-known/agent-card.json"
AGENT_CARD_ALIAS_PATH = "/.well-known/agent.json"
A2A_RPC_PATH = "/"

A2A_METHOD_SEND = "SendMessage"
A2A_METHOD_GET_TASK = "GetTask"
A2A_METHOD_CANCEL_TASK = "CancelTask"
A2A_METHODS = frozenset(
    {A2A_METHOD_SEND, A2A_METHOD_GET_TASK, A2A_METHOD_CANCEL_TASK}
)

# Standard JSON-RPC 2.0 error codes.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# A2A-specific error codes (spec section 5.4).
A2A_TASK_NOT_FOUND = -32001
A2A_TASK_NOT_CANCELABLE = -32002
A2A_PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
A2A_UNSUPPORTED_OPERATION = -32004
A2A_CONTENT_TYPE_NOT_SUPPORTED = -32005
A2A_INVALID_AGENT_RESPONSE = -32006
A2A_EXTENDED_AGENT_CARD_NOT_CONFIGURED = -32007
A2A_EXTENSION_SUPPORT_REQUIRED = -32008
A2A_VERSION_NOT_SUPPORTED = -32009

# Internal (database / state machine) names. Do not rename these without a
# migration; the wire mapping below is the only place A2A spellings appear.
TASK_STATES = (
    "submitted",
    "working",
    "input-required",
    "completed",
    "canceled",
    "failed",
    "rejected",
)
TERMINAL_STATES = frozenset({"completed", "canceled", "failed", "rejected"})
INTERRUPTED_STATES = frozenset({"input-required"})

ROLES = ("user", "agent")

A2A_STATE_BY_INTERNAL = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "canceled": "TASK_STATE_CANCELED",
    "failed": "TASK_STATE_FAILED",
    "rejected": "TASK_STATE_REJECTED",
}
INTERNAL_STATE_BY_A2A = {
    wire: internal for internal, wire in A2A_STATE_BY_INTERNAL.items()
}

A2A_ROLE_BY_INTERNAL = {"user": "ROLE_USER", "agent": "ROLE_AGENT"}
INTERNAL_ROLE_BY_A2A = {
    wire: internal for internal, wire in A2A_ROLE_BY_INTERNAL.items()
}

# ---------------------------------------------------------------------------
# Stable reason strings (tests reference these keys, never literals)
# ---------------------------------------------------------------------------

REASONS = {
    "R4": "server busy",
    "R6A": "messageId already used in a different task",
    "R6D": "turn in progress for this message",
    "R7": "task is terminal",
    "R8": "task is busy",
    "R9": "round mismatch",
    "R10": "accept mismatch",
    "RESUME_DRIFT": "resume round drift",
}

# ---------------------------------------------------------------------------
# LLM / credentials (values come from .env; never logged)
# ---------------------------------------------------------------------------

NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL = os.getenv("NIM_MODEL", "")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
A2A_TOKEN = os.getenv("A2A_TOKEN") or None
