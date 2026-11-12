"""Tests for the frozen constants in src/config.py."""

from __future__ import annotations

import re

import pytest

from src import config

pytestmark = pytest.mark.fast


def test_network_addresses() -> None:
    assert config.SELLER_HOST == "127.0.0.1"
    assert config.SELLER_PORT == 8002
    assert config.SELLER_URL == "http://127.0.0.1:8002"


def test_caps() -> None:
    assert config.MAX_ROUNDS == 8
    assert config.MAX_MESSAGE_BYTES == 16_384
    assert config.MAX_OPEN_TASKS == 20
    assert config.MAX_HISTORY_RESPONSE == 20
    assert config.MAX_RATIONALE_CHARS == 400
    assert config.MAX_ROUNDS > 0
    assert config.MAX_OPEN_TASKS > 0
    assert 0 < config.MAX_HISTORY_RESPONSE <= 20


def test_curve_parameters() -> None:
    assert config.START_U == 0.90
    assert config.END_U == 0.10
    assert config.BETA_DEFAULT == 2.0
    assert 0 <= config.END_U < config.START_U <= 1
    assert config.BETA_DEFAULT > 0


def test_band_parameters() -> None:
    assert config.BAND_LOW == 0.05
    assert config.BAND_HIGH == 0.10
    assert config.BAND_LOW <= config.BAND_HIGH


def test_band_low_at_least_eps_rounding() -> None:
    # X6 floor: the band's lower edge may never be tighter than the lattice's
    # own rounding error, or a fallback offer could fail its own band.
    assert config.BAND_LOW >= config.EPS_ROUNDING


def test_epsilon_values() -> None:
    assert config.EPS_ROUNDING == 0.03
    assert config.EPS_MONOTONE == 0.02
    assert config.EPS_BAND == 0.03
    assert 0 <= config.EPS_ROUNDING < 1
    assert 0 <= config.EPS_MONOTONE < 1
    assert 0 <= config.EPS_BAND < 1


def test_timeout_ordering() -> None:
    assert config.LLM_TIMEOUT == 30.0
    assert config.BLOCKING_TIMEOUT == 45.0
    assert config.HTTP_TIMEOUT == 60.0
    assert config.LLM_TIMEOUT < config.BLOCKING_TIMEOUT < config.HTTP_TIMEOUT
    assert config.MAX_RETRY_TOTAL == 15.0
    assert config.POLL_MAX == 60.0
    assert config.POLL_INTERVAL == 2.0


def test_retry_and_trace_config() -> None:
    assert config.LLM_RETRY_ATTEMPTS == 1
    assert config.LLM_TEMPERATURE == 0.3
    assert config.TRACE_U_DECIMALS == 2
    assert config.PRICE_STEP == 10
    assert config.SQLITE_BUSY_TIMEOUT_MS == 5_000


def test_reasons_are_stable_and_short() -> None:
    expected = {
        "R4": "server busy",
        "R6A": "messageId already used in a different task",
        "R6D": "turn in progress for this message",
        "R7": "task is terminal",
        "R8": "task is busy",
        "R9": "round mismatch",
        "R10": "accept mismatch",
        "RESUME_DRIFT": "resume round drift",
    }
    assert expected == config.REASONS
    for reason in config.REASONS.values():
        assert len(reason) <= 60
    assert len(set(config.REASONS.values())) == len(config.REASONS)


def test_protocol_version_parses_as_major_minor() -> None:
    assert config.A2A_PROTOCOL_VERSION == "1.0"
    assert re.fullmatch(r"\d+\.\d+", config.A2A_PROTOCOL_VERSION)
    assert config.A2A_SPEC_VERSION == "1.0.0"
    assert config.A2A_VERSION_HEADER == "A2A-Version"


def test_jsonrpc_methods_are_pinned() -> None:
    assert config.A2A_METHOD_SEND == "SendMessage"
    assert config.A2A_METHOD_GET_TASK == "GetTask"
    assert config.A2A_METHOD_CANCEL_TASK == "CancelTask"
    assert {"SendMessage", "GetTask", "CancelTask"} == config.A2A_METHODS


def test_state_mapping_is_complete_and_round_trips() -> None:
    assert set(config.A2A_STATE_BY_INTERNAL) == set(config.TASK_STATES)
    for internal, wire in config.A2A_STATE_BY_INTERNAL.items():
        assert wire.startswith("TASK_STATE_")
        assert config.INTERNAL_STATE_BY_A2A[wire] == internal
    assert config.A2A_STATE_BY_INTERNAL["input-required"] == (
        "TASK_STATE_INPUT_REQUIRED"
    )
    assert config.A2A_STATE_BY_INTERNAL["canceled"] == "TASK_STATE_CANCELED"


def test_role_mapping_is_pinned() -> None:
    assert config.A2A_ROLE_BY_INTERNAL == {
        "user": "ROLE_USER",
        "agent": "ROLE_AGENT",
    }
    assert config.INTERNAL_ROLE_BY_A2A["ROLE_USER"] == "user"


def test_terminal_and_interrupted_states() -> None:
    assert {
        "completed",
        "canceled",
        "failed",
        "rejected",
    } == config.TERMINAL_STATES
    assert {"input-required"} == config.INTERRUPTED_STATES
    assert set(config.TASK_STATES) >= config.TERMINAL_STATES


def test_error_codes() -> None:
    assert config.JSONRPC_PARSE_ERROR == -32700
    assert config.JSONRPC_INVALID_REQUEST == -32600
    assert config.JSONRPC_METHOD_NOT_FOUND == -32601
    assert config.JSONRPC_INVALID_PARAMS == -32602
    assert config.JSONRPC_INTERNAL_ERROR == -32603
    assert config.A2A_TASK_NOT_FOUND == -32001
    assert config.A2A_TASK_NOT_CANCELABLE == -32002
    assert config.A2A_UNSUPPORTED_OPERATION == -32004
    assert config.A2A_VERSION_NOT_SUPPORTED == -32009
