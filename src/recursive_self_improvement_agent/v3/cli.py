"""`rasis-v3`: run the v3 harness-ratchet study and inspect its registry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import CHECKPOINT, CallStore, ChunkDeadline
from .registry import Registry
from .report import write_markdown
from .stats import mcnemar_sample_size
from .study import ARMS, Study, StudyConfig


def _write(path: Path | None, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if path is None:
        print(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rasis-v3", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run all arms, the audits and the sealed final")
    run.add_argument("--provider", choices=["simulated", "ollama"], default="simulated")
    run.add_argument("--proposer", choices=["rules", "ollama"], default="rules")
    run.add_argument("--small-model", default="llama3.2:3b")
    run.add_argument("--large-model", default="qwen2.5:7b")
    run.add_argument("--n-dev", type=int, default=30)
    run.add_argument("--n-selection", type=int, default=80)
    run.add_argument("--n-final", type=int, default=40)
    run.add_argument("--cycles", type=int, default=4)
    run.add_argument("--candidates", type=int, default=2)
    run.add_argument("--arms", default=",".join(ARMS))
    run.add_argument("--approval", choices=["standing", "pending"], default="pending")
    run.add_argument("--reviewer", default="local-reviewer")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--db", type=Path, default=Path("data/v3-registry.sqlite3"))
    run.add_argument("--out", type=Path, default=Path("results/v3"))
    run.add_argument(
        "--cache",
        type=Path,
        help="durable call store; makes the run resumable (re-run the same command)",
    )
    run.add_argument(
        "--max-seconds",
        type=float,
        help="time box per invocation; exits with code 3 and resumes on re-run",
    )
    run.add_argument("--max-calls", type=int, help="new live model calls per invocation")

    hist = sub.add_parser("history", help="activation history and lineage for an arm")
    hist.add_argument("--db", type=Path, required=True)
    hist.add_argument("--arm", default="ratchet")
    hist.add_argument("--as-of", type=int)

    pack = sub.add_parser("pack", help="reconstruction pack for a review event")
    pack.add_argument("--db", type=Path, required=True)
    pack.add_argument("--arm", default="ratchet")
    pack.add_argument("--review-seq", type=int, required=True)

    prov = sub.add_parser("prov", help="export PROV-JSON lineage")
    prov.add_argument("--db", type=Path, required=True)
    prov.add_argument("--out", type=Path)

    rollback = sub.add_parser("rollback", help="re-activate a previously accepted harness")
    rollback.add_argument("--db", type=Path, required=True)
    rollback.add_argument("--arm", default="ratchet")
    rollback.add_argument("--to", required=True)
    rollback.add_argument("--reviewer", required=True)
    rollback.add_argument("--reason", required=True)

    power = sub.add_parser("power", help="paired sample size for a target accuracy gain")
    power.add_argument("--delta", type=float, default=0.15)
    power.add_argument("--discordance", type=float, default=0.30)

    args = parser.parse_args(argv)
    if args.command == "run":
        if args.db.exists():
            parser.error(f"{args.db} exists; use a fresh registry path per study")
        config = StudyConfig(
            provider=args.provider,
            proposer=args.proposer,
            small_model=args.small_model,
            large_model=args.large_model,
            n_dev=args.n_dev,
            n_selection=args.n_selection,
            n_final=args.n_final,
            cycles=args.cycles,
            candidates_per_cycle=args.candidates,
            arms=tuple(a for a in args.arms.split(",") if a),
            approval=args.approval,
            reviewer=args.reviewer,
            seed=args.seed,
        )
        CHECKPOINT.reset(
            CallStore(args.cache) if args.cache else None, args.max_seconds, args.max_calls
        )
        registry = Registry(":memory:") if args.cache else Registry(args.db)
        try:
            report = Study(config, registry).run()
        except ChunkDeadline:
            stored = CHECKPOINT.store.count() if CHECKPOINT.store else 0
            print(f"checkpoint: {stored} calls stored in {args.cache}; re-run to resume")
            return 3
        if args.cache:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(args.db)) as target:
                registry.db.backup(target)
            report["checkpoint"] = {
                "cache": str(args.cache),
                "stored_calls": CHECKPOINT.store.count(),
            }
        _write(args.out / "report.json", report)
        write_markdown(report, args.out / "summary.md")
        print(f"wrote {args.out / 'report.json'} and {args.out / 'summary.md'}")
    elif args.command == "history":
        registry = Registry(args.db)
        registry.clock = 10**9
        payload: dict[str, Any] = {
            "history": registry.history(args.arm),
            "chain_valid": registry.verify_chain(),
        }
        if args.as_of is not None:
            payload["active_as_of"] = registry.as_of(args.arm, args.as_of)
        _write(None, payload)
    elif args.command == "pack":
        _write(None, Registry(args.db).reconstruction_pack(args.arm, args.review_seq))
    elif args.command == "prov":
        _write(args.out, Registry(args.db).prov())
    elif args.command == "rollback":
        registry = Registry(args.db)
        registry.clock = max((e["tx_time"] for e in registry.events()), default=0)
        registry.rollback(args.arm, args.to, args.reviewer, args.reason)
        _write(None, {"active": registry.active(args.arm), "history": registry.history(args.arm)})
    elif args.command == "power":
        print(mcnemar_sample_size(args.delta, args.discordance))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
