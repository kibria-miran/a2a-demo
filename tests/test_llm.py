"""llm.py: LLMError mapping, token accounting, default_chat wiring."""

from __future__ import annotations

import pytest

from src import config, llm

pytestmark = pytest.mark.fast


@pytest.fixture
def with_nim_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "NIM_MODEL", "fake-model")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "dummy")
    yield


@pytest.fixture
def without_nim_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "NIM_MODEL", "")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    yield


def test_default_chat_none_without_credentials(without_nim_env) -> None:
    assert llm.default_chat() is None


def test_default_chat_returns_chat_with_credentials(with_nim_env) -> None:
    assert llm.default_chat() is llm.chat


def test_chat_maps_network_failures_to_llmerror(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding_client() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm, "_get_client", exploding_client)
    monkeypatch.setattr(config, "NIM_MODEL", "fake-model")
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}], role="proposer")


def test_chat_maps_malformed_envelope_to_llmerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        choices: list[object] = []

    class _Completions:
        @staticmethod
        def create(**kwargs: object) -> object:
            return _Response()

    class _Client:
        def __init__(self) -> None:
            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

    monkeypatch.setattr(llm, "_get_client", lambda: _Client())
    monkeypatch.setattr(config, "NIM_MODEL", "fake-model")
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}], role="proposer")


def test_build_app_wires_chat_when_credentials_set(
    with_nim_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from src import main_seller

    args = argparse.Namespace(
        profile="profiles/seller.json",
        db=str(tmp_path / "seller.db"),
        host="127.0.0.1",
        port=0,
        token=None,
    )
    app = main_seller.build_app(args)
    assert app.state.chat is llm.chat


def test_build_app_leaves_chat_none_without_credentials(
    without_nim_env, tmp_path
) -> None:
    import argparse

    from src import main_seller

    args = argparse.Namespace(
        profile="profiles/seller.json",
        db=str(tmp_path / "seller.db"),
        host="127.0.0.1",
        port=0,
        token=None,
    )
    app = main_seller.build_app(args)
    assert app.state.chat is None
