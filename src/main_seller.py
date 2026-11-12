"""Uvicorn entry point for the seller."""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI

from src import config, llm, trace
from src.server import create_app
from src.store import Store
from src.utility import load_profile


def build_app(args: argparse.Namespace) -> FastAPI:
    trace.configure(config.TRACE_PATH)
    store = Store(args.db, journal_mode="WAL")
    profile = load_profile(args.profile)
    # None when NIM_MODEL/NVIDIA_API_KEY are unset: fallback-only mode.
    return create_app(
        store,
        profile,
        token=args.token,
        card_url=f"http://{args.host}:{args.port}",
        chat=llm.default_chat(),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="main_seller")
    parser.add_argument("--profile", default="profiles/seller.json")
    parser.add_argument("--db", default="data/seller.db")
    parser.add_argument("--host", default=config.SELLER_HOST)
    parser.add_argument("--port", type=int, default=config.SELLER_PORT)
    parser.add_argument("--token", default=config.A2A_TOKEN)
    args = parser.parse_args(argv)
    app = build_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
