#!/usr/bin/env python3
"""Machine-checked negotiation experiments (section 13.3).

Runs negotiations between two in-process agents and reports machine-checkable
metrics: outcome, rounds to agreement, utilities, joint utility, Nash
product, surplus-split fairness, wall-clock latency.

Modes:
  offline  deterministic lattice fallback on both sides (default).
           Fully offline; this is what runs in CI.
  live     LLM proposer on both sides (buyer = LLM fallback-marked anyway,
           requires NIM_MODEL/NVIDIA_API_KEY in .env; X2/X3/X5).

Usage:
  .venv/bin/python evals/run_evals.py --experiment x1 --runs 10
  .venv/bin/python evals/run_evals.py --experiment x4 --runs 10
  .venv/bin/python evals/run_evals.py --experiment x6 --runs 10
  .venv/bin/python evals/run_evals.py --experiment x2 --mode live --runs 20
  .venv/bin/python evals/run_evals.py --experiment x3 --mode live --runs 20
  .venv/bin/python evals/run_evals.py --experiment x5 --mode live --runs 20 \\
      --seller-model nvidia/llama-3.3-nemotron-super-49b-v1 \\
      --buyer-model meta/llama-3.1-8b-instruct
  .venv/bin/python evals/run_evals.py --all-offline --runs 10

Experiments:
  x1  Beta grid (3.0 / 1.0 / 0.5) x 3x3 matrix — buyer_beta x seller_beta
  x2  LLM proposer on both sides vs pure fallback (live only)
  x3  Rationale text on the wire vs suppressed (live only)
  x4  MAX_ROUNDS 4 / 8 / 12
  x5  One model on both sides vs a different model per side (live only)
  x6  BAND_LOW/BAND_HIGH 0.03 / 0.05 / 0.10

X2/X3/X5 write combined files (x2-compare, x3-rationale, x5-models) so both
arms of each comparison sit side by side. Per-run token counts are recorded.

Results are machine-checkable and intended to be summarized in README.md.
Raw per-run lines go to evals/results/<experiment>-<mode>.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import loopback_client

from src import config, llm
from src.buyer_cli import negotiate
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"

BUYER_BASE: dict[str, Any] = json.loads((ROOT / "profiles" / "buyer.json").read_text())
SELLER_BASE: dict[str, Any] = json.loads((ROOT / "profiles" / "seller.json").read_text())


def make_profile(base: dict[str, Any], **overrides: Any) -> Profile:
    data = json.loads(json.dumps(base))
    data.update(overrides)
    return Profile.load(data)


class MissingCredentials(RuntimeError):
    """The live arm was requested without a usable NIM configuration."""


@dataclass
class RunResult:
    experiment: str
    mode: str
    run_id: str
    outcome: str
    terms: dict[str, int] | None
    rounds: int
    buyer_u: float | None
    seller_u: float | None
    joint_u: float | None
    nash_product: float | None
    surplus_fairness: float | None
    latency_s: float
    source: str
    tokens_in: int = 0
    tokens_out: int = 0
    seed: dict[str, Any] = field(default_factory=dict)


async def run_one(
    experiment: str,
    mode: str,
    run_id: str,
    *,
    buyer_overrides: dict[str, Any] | None = None,
    seller_overrides: dict[str, Any] | None = None,
    max_rounds: int | None = None,
    band_low: float | None = None,
    band_high: float | None = None,
    seller_chat: Any | None = None,
    buyer_chat: Any | None = None,
    include_rationale: bool = True,
    buyer_model: str | None = None,
    seller_model: str | None = None,
) -> RunResult:
    buyer = make_profile(BUYER_BASE, **(buyer_overrides or {}))
    seller = make_profile(SELLER_BASE, **(seller_overrides or {}))

    old_max, old_low, old_high = config.MAX_ROUNDS, config.BAND_LOW, config.BAND_HIGH
    if max_rounds is not None:
        config.MAX_ROUNDS = max_rounds
    if band_low is not None:
        config.BAND_LOW = band_low
    if band_high is not None:
        config.BAND_HIGH = band_high

    tmpdir = tempfile.mkdtemp(prefix="eval-")
    store = Store(str(Path(tmpdir) / "seller.db"), journal_mode="MEMORY")
    app = None
    if mode == "fallback":
        # No chat wired: deterministic fallback on both sides.
        app = create_app(
            store,
            seller,
            card_url="http://testserver",
            include_rationale=include_rationale,
        )
    elif mode == "live":
        seller_chat = seller_chat or llm.default_chat(seller_model)
        buyer_chat = buyer_chat or llm.default_chat(buyer_model)
        if seller_chat is None or buyer_chat is None:
            raise MissingCredentials(
                "live mode needs NVIDIA_API_KEY and a model "
                "(NIM_MODEL or --seller-model/--buyer-model) in .env"
            )
        app = create_app(
            store,
            seller,
            chat=seller_chat,
            card_url="http://testserver",
            include_rationale=include_rationale,
        )
    else:
        raise AssertionError(mode)

    tokens_before = llm.token_usage_all()
    started = time.perf_counter()
    try:
        async with loopback_client(app) as loop:
            client = A2AClient("http://testserver", http_client=loop)
            outcome = await negotiate(
                client,
                buyer,
                verbose=False,
                chat=buyer_chat,
                include_rationale=include_rationale,
            )
    finally:
        latency = time.perf_counter() - started
        config.MAX_ROUNDS, config.BAND_LOW, config.BAND_HIGH = old_max, old_low, old_high
    tokens_in, tokens_out = llm.usage_delta(tokens_before)

    try:
        view = store.task_view(outcome["task_id"]) if outcome.get("task_id") else None
    except Exception:
        view = None

    terms = outcome.get("terms")
    buyer_u: float | None = None
    seller_u: float | None = None
    rounds = 0

    if terms is not None:
        buyer_u = buyer.utility(terms)
        seller_u = seller.utility(terms)

    if view is not None:
        for message in view.get("history") or []:
            for part in message.get("parts") or []:
                data = part.get("data")
                if isinstance(data, dict) and data.get("type") in {"offer", "counter"}:
                    rounds += 1

    joint = (buyer_u + seller_u) if buyer_u is not None and seller_u is not None else None
    nash = (buyer_u * seller_u) if buyer_u is not None and seller_u is not None else None

    store.close()
    try:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass

    return RunResult(
        experiment=experiment,
        mode=mode,
        run_id=run_id,
        outcome=outcome.get("outcome", "unknown"),
        terms=terms,
        rounds=rounds,
        buyer_u=buyer_u,
        seller_u=seller_u,
        joint_u=joint,
        nash_product=nash,
        surplus_fairness=(buyer_u / (buyer_u + seller_u))
        if buyer_u is not None and seller_u is not None and (buyer_u + seller_u) > 0
        else None,
        latency_s=round(latency, 3),
        source="llm" if mode == "live" else "fallback",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        seed={
            "buyer_overrides": buyer_overrides or {},
            "seller_overrides": seller_overrides or {},
            "max_rounds": max_rounds,
            "band_low": band_low,
            "band_high": band_high,
            "include_rationale": include_rationale,
            "buyer_model": buyer_model,
            "seller_model": seller_model,
        },
    )


def summarize(name: str, results: list[RunResult]) -> None:
    print(f"\n## {name} ({len(results)} runs)")
    completed = [r for r in results if r.outcome == "completed"]
    print(f"agreement rate: {len(completed) / max(1, len(results)):.2f} "
          f"({len(completed)}/{len(results)})")
    for label, key in [
        ("buyer utility", "buyer_u"),
        ("seller utility", "seller_u"),
        ("joint utility", "joint_u"),
        ("nash product", "nash_product"),
        ("surplus fairness", "surplus_fairness"),
        ("latency s", "latency_s"),
    ]:
        vals = [getattr(r, key) for r in completed]
        vals = [v for v in vals if v is not None]
        if vals:
            print(f"{label}: mean={sum(vals)/len(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}")
    tokens_in = sum(r.tokens_in for r in results)
    tokens_out = sum(r.tokens_out for r in results)
    if tokens_in or tokens_out:
        print(f"tokens: in={tokens_in} out={tokens_out} per-run={tokens_in // max(1, len(results))}")


def write_jsonl(mode: str, results: list[RunResult], name: str | None = None) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{name or f'{results[0].experiment}-{mode}'}.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result.__dict__, default=str) + "\n")
    return out


def run_x1(mode: str, runs: int) -> None:
    results: list[RunResult] = []
    for buyer_beta in (3.0, 1.0, 0.5):
        for seller_beta in (3.0, 1.0, 0.5):
            for index in range(runs):
                results.append(
                    asyncio.run(
                        run_one(
                            "x1",
                            mode,
                            f"bb{buyer_beta}-sb{seller_beta}-{index}",
                            buyer_overrides={"beta": buyer_beta},
                            seller_overrides={"beta": seller_beta},
                        )
                    )
                )
    out = write_jsonl(mode, results)
    print(f"saved {out}")
    for buyer_beta in (3.0, 1.0, 0.5):
        for seller_beta in (3.0, 1.0, 0.5):
            cell = [
                r
                for r in results
                if r.seed.get("buyer_overrides", {}).get("beta") == buyer_beta
                and r.seed.get("seller_overrides", {}).get("beta") == seller_beta
            ]
            completed = [r for r in cell if r.outcome == "completed" and r.joint_u is not None]
            rate = len(completed) / len(cell)
            joint = sum(r.joint_u for r in completed) / max(1, len(completed))
            print(f"buyer_beta={buyer_beta} seller_beta={seller_beta}: "
                  f"agree={rate:.2f} joint={joint:.3f}")
    summarize("X1 beta grid (fallback)" if mode == "fallback" else "X1 beta grid (live)", results)


def run_x4(mode: str, runs: int) -> None:
    results: list[RunResult] = []
    for max_rounds in (4, 8, 12):
        for index in range(runs):
            results.append(
                asyncio.run(
                    run_one(
                        "x4",
                        mode,
                        f"T{max_rounds}-{index}",
                        max_rounds=max_rounds,
                    )
                )
            )
    out = write_jsonl(mode, results)
    print(f"saved {out}")
    summarize(f"X4 MAX_ROUNDS in {{4,8,12}} ({mode})", results)


def run_x6(mode: str, runs: int) -> None:
    results: list[RunResult] = []
    for band in (0.03, 0.05, 0.10):
        for index in range(runs):
            results.append(
                asyncio.run(
                    run_one(
                        "x6",
                        mode,
                        f"band{band}-{index}",
                        band_low=band,
                        band_high=band,
                    )
                )
            )
    out = write_jsonl(mode, results)
    print(f"saved {out}")
    if mode == "fallback":
        print(
            "note: the band gate validates the LLM proposer only; the offline "
            "lattice runs bypass V3, so the arms are identical by construction. "
            "Use --mode live for a meaningful X6."
        )
    summarize("X6 band widths", results)


def _require_live(seller_model: str | None = None, buyer_model: str | None = None) -> None:
    """Fail fast and actionably instead of emitting a traceback."""

    if not config.NVIDIA_API_KEY:
        raise SystemExit(
            "live mode needs NVIDIA_API_KEY (copy .env.example to .env and fill it in)"
        )
    if not (seller_model or config.NIM_MODEL) or not (buyer_model or config.NIM_MODEL):
        raise SystemExit(
            "live mode needs a model id: NIM_MODEL in .env, or --seller-model/--buyer-model"
        )


def _run_arm(
    experiment: str,
    arm: str,
    mode: str,
    runs: int,
    **kwargs: Any,
) -> list[RunResult]:
    return [
        asyncio.run(run_one(experiment, mode, f"{arm}-{index}", **kwargs))
        for index in range(runs)
    ]


def run_x2(mode: str, runs: int, *, seller_model: str | None = None,
           buyer_model: str | None = None) -> None:
    """LLM proposer on both sides vs the pure lattice fallback."""

    if mode != "live":
        raise SystemExit(
            "X2 compares the LLM against fallback; use --mode live with NIM keys"
        )
    _require_live(seller_model, buyer_model)
    results: list[RunResult] = []
    results += _run_arm("x2", "fallback", "fallback", runs)
    results += _run_arm(
        "x2", "llm_both", "live", runs,
        seller_model=seller_model, buyer_model=buyer_model,
    )
    out = write_jsonl(mode, results, name="x2-compare")
    print(f"saved {out}")
    for arm in ("fallback", "llm_both"):
        summarize(f"X2 arm={arm} (live)", [r for r in results if r.run_id.startswith(arm)])


def run_x3(mode: str, runs: int, *, seller_model: str | None = None,
           buyer_model: str | None = None) -> None:
    """Rationale text sent vs suppressed on the wire (LLM arms only)."""

    if mode != "live":
        raise SystemExit(
            "X3 needs a real proposer; use --mode live with NIM keys"
        )
    _require_live(seller_model, buyer_model)
    results: list[RunResult] = []
    for label, enabled in (("rationale_on", True), ("rationale_off", False)):
        arm = _run_arm(
            "x3", label, "live", runs,
            include_rationale=enabled,
            seller_model=seller_model, buyer_model=buyer_model,
        )
        results += arm
        summarize(f"X3 arm={label} (live)", arm)
    out = write_jsonl(mode, results, name="x3-rationale")
    print(f"saved {out}")


def run_x5(mode: str, runs: int, *, seller_model: str | None = None,
           buyer_model: str | None = None) -> None:
    """Same model on both sides vs two different NIM models."""

    if mode != "live":
        raise SystemExit(
            "X5 compares real NIM models; use --mode live with NIM keys"
        )
    seller_model = seller_model or config.NIM_MODEL
    buyer_model = buyer_model or os.getenv("NIM_MODEL_B", "")
    if not seller_model or not buyer_model:
        raise SystemExit(
            "X5 needs two model ids: --seller-model/--buyer-model "
            "or NIM_MODEL/NIM_MODEL_B in .env"
        )
    if seller_model == buyer_model:
        raise SystemExit("X5 needs two DIFFERENT model ids")
    _require_live(seller_model, buyer_model)
    results: list[RunResult] = []
    for label, left, right in (
        ("same_model", seller_model, seller_model),
        ("distinct_models", seller_model, buyer_model),
    ):
        arm = _run_arm(
            "x5", label, "live", runs,
            seller_model=left, buyer_model=right,
        )
        results += arm
        summarize(f"X5 arm={label} seller={left} buyer={right}", arm)
    out = write_jsonl(mode, results, name="x5-models")
    print(f"saved {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_evals")
    parser.add_argument(
        "--experiment", choices=["x1", "x2", "x3", "x4", "x5", "x6"], required=False
    )
    parser.add_argument("--mode", choices=["fallback", "live"], default="fallback")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--seller-model", default=None, help="X2/X3/X5 seller model id")
    parser.add_argument(
        "--buyer-model",
        default=None,
        help="X2/X3/X5 buyer model id (X5 defaults to $NIM_MODEL_B)",
    )
    parser.add_argument("--all-offline", action="store_true", help="run x1, x4 and x6 in fallback mode")
    args = parser.parse_args()

    if args.all_offline:
        run_x1("fallback", args.runs)
        run_x4("fallback", args.runs)
        run_x6("fallback", args.runs)
        return
    try:
        if args.experiment == "x1":
            run_x1(args.mode, args.runs)
        elif args.experiment == "x2":
            run_x2(args.mode, args.runs,
                   seller_model=args.seller_model, buyer_model=args.buyer_model)
        elif args.experiment == "x3":
            run_x3(args.mode, args.runs,
                   seller_model=args.seller_model, buyer_model=args.buyer_model)
        elif args.experiment == "x4":
            run_x4(args.mode, args.runs)
        elif args.experiment == "x5":
            run_x5(args.mode, args.runs,
                   seller_model=args.seller_model, buyer_model=args.buyer_model)
        elif args.experiment == "x6":
            run_x6(args.mode, args.runs)
    except MissingCredentials as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
