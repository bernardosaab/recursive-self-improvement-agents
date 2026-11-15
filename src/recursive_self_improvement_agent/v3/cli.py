"""`rasis-v3`: run the v3 harness-ratchet study and inspect its registry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import CHECKPOINT, CallStore, ChunkDeadline
from .redteam import evaluate as redteam_evaluate
from .redteam import render as redteam_render
from .registry import Registry
from .report import write_markdown
from .robustness import (
    confirm,
    render_confirm,
    render_reprice,
    render_sweep,
    reprice,
    sweep,
    verify_commitment,
)
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
    run.add_argument("--task-seed-offset", type=int, default=0)
    run.add_argument("--proposer-model", help="Ollama proposer model (default: large tier)")
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

    sw = sub.add_parser("sweep", help="offline sensitivity sweep over seeds and guide strength")
    sw.add_argument("--seeds", default="0,1,2,3,4")
    sw.add_argument("--guide-effects", default="1.0,0.6,0.2")
    sw.add_argument("--arms", default="fixed,ratchet,prompt_only,random")
    sw.add_argument("--error-scales", default="1.0", help="multipliers on simulated error rates")
    sw.add_argument("--out", type=Path, default=Path("results/v3-sweep"))

    cf = sub.add_parser("confirm", help="evaluate frozen harnesses from a report on fresh tasks")
    cf.add_argument("--report", type=Path, required=True)
    cf.add_argument("--arms", default="fixed,ratchet")
    cf.add_argument("--n-final", type=int, default=40)
    cf.add_argument("--task-seed-offset", type=int, default=100)
    cf.add_argument("--out", type=Path, default=Path("results/v3-confirm"))
    cf.add_argument("--cache", type=Path)
    cf.add_argument("--max-seconds", type=float)
    cf.add_argument("--small-model")
    cf.add_argument("--large-model")
    cf.add_argument("--variants", default="small_only")
    cf.add_argument("--power", action="store_true", help="sample GPU power with nvidia-smi")

    rp = sub.add_parser("reprice", help="cost per verified completion under several price sheets")
    rp.add_argument("--report", type=Path, required=True)
    rp.add_argument("--out", type=Path, default=Path("results/v3-reprice"))

    rt = sub.add_parser("redteam", help="measured catch rate of the heuristic text boundaries")
    rt.add_argument("--out", type=Path, default=Path("results/v3-redteam"))

    vc = sub.add_parser("verify-commitment", help="check a revealed seed against its commitment")
    vc.add_argument("--commitment", type=Path, required=True)
    vc.add_argument("--offset", type=int, required=True)
    vc.add_argument("--salt", required=True)

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
            task_seed_offset=args.task_seed_offset,
            proposer_model=args.proposer_model,
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
    elif args.command == "sweep":
        result = sweep(
            [int(x) for x in args.seeds.split(",")],
            [float(x) for x in args.guide_effects.split(",")],
            tuple(args.arms.split(",")),
            error_scales=[float(x) for x in args.error_scales.split(",")],
        )
        _write(args.out / "sweep.json", result)
        (args.out / "summary.md").write_text(render_sweep(result), encoding="utf-8")
        print(render_sweep(result))
    elif args.command == "confirm":
        CHECKPOINT.reset(CallStore(args.cache) if args.cache else None, args.max_seconds)
        try:
            result = confirm(
                args.report,
                args.arms.split(","),
                args.n_final,
                args.task_seed_offset,
                small_model=args.small_model,
                large_model=args.large_model,
                variants=tuple(v for v in args.variants.split(",") if v),
                measure_power=args.power,
            )
        except ChunkDeadline:
            print(f"checkpoint: {CHECKPOINT.store.count()} calls stored; re-run to resume")
            return 3
        _write(args.out / "confirm.json", result)
        (args.out / "summary.md").write_text(render_confirm(result), encoding="utf-8")
        print(render_confirm(result))
    elif args.command == "reprice":
        result = reprice(args.report)
        _write(args.out / "reprice.json", result)
        (args.out / "summary.md").write_text(render_reprice(result), encoding="utf-8")
        print(render_reprice(result))
    elif args.command == "redteam":
        result = redteam_evaluate()
        _write(args.out / "redteam.json", result)
        (args.out / "summary.md").write_text(redteam_render(result), encoding="utf-8")
        print(redteam_render(result))
    elif args.command == "verify-commitment":
        ok = verify_commitment(args.commitment, args.offset, args.salt)
        print("commitment verified" if ok else "commitment MISMATCH")
        return 0 if ok else 1
    elif args.command == "power":
        print(mcnemar_sample_size(args.delta, args.discordance))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
