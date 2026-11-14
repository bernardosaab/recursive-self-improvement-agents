from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .data import SPLIT_SEEDS, build_split
from .evaluation import compare_with_heuristic, evaluate_pair
from .improvement import (
    DeterministicFakeProvider,
    OpenAICompatibleProvider,
    apply_proposal,
    build_diagnostics,
    validate_proposal,
)
from .models import RuntimeConfig
from .store import Registry
from .study import Study

BASELINE = RuntimeConfig(
    retrieval_strategy="single_pass",
    max_evidence_items=1,
    verify_before_answer=False,
)
HEURISTIC = RuntimeConfig(
    retrieval_strategy="cross_check",
    max_evidence_items=2,
    verify_before_answer=True,
)


def run_demo(provider_name: str, db_path: Path, output_path: Path | None) -> dict[str, Any]:
    registry = Registry(db_path)
    registry.require_tool_budget(198)
    base_id = registry.add_version(BASELINE)
    registry.register_initial(base_id)
    dev_cases = build_split("development")
    dev_report, dev_traces, _ = evaluate_pair(
        base_id,
        BASELINE,
        base_id,
        BASELINE,
        dev_cases,
        split="development",
        seed=SPLIT_SEEDS["development"],
    )
    registry.add_report(dev_report)
    diagnostics = build_diagnostics(dev_traces)
    provider = (
        DeterministicFakeProvider()
        if provider_name == "fake"
        else OpenAICompatibleProvider.from_environment()
    )
    proposal, model_usage = provider.propose(diagnostics)
    validate_proposal(proposal, diagnostics)
    candidate = apply_proposal(BASELINE, proposal)
    candidate_id = registry.add_version(candidate, base_id)

    heuristic_id = registry.add_version(HEURISTIC, base_id)
    selection = build_split("selection")
    report, candidate_results, baseline_results = evaluate_pair(
        candidate_id,
        candidate,
        base_id,
        BASELINE,
        selection,
        split="selection",
        seed=SPLIT_SEEDS["selection"],
        model_usage=model_usage,
    )
    heuristic_report, _, _ = evaluate_pair(
        heuristic_id,
        HEURISTIC,
        base_id,
        BASELINE,
        selection,
        split="selection",
        seed=SPLIT_SEEDS["selection"],
    )
    registry.add_report(heuristic_report)
    report = compare_with_heuristic(report, heuristic_report)
    registry.add_report(report)

    summary = {
        "generator_version": "synthetic-v1",
        "split_seeds": SPLIT_SEEDS,
        "development_tasks": len(dev_cases),
        "development_failures": len(diagnostics),
        "development_report": dev_report.model_dump(mode="json"),
        "development_diagnostic_references": [item["task_id"] for item in diagnostics],
        "candidate_diff": proposal.changes(),
        "candidate_rationale": proposal.rationale,
        "candidate_failure_references": proposal.failure_references,
        "candidate_version_id": candidate_id,
        "heuristic_version_id": heuristic_id,
        "selection_report": report.model_dump(mode="json"),
        "heuristic_selection_report": heuristic_report.model_dump(mode="json"),
        "selection_candidate_correct": sum(result.correct for result in candidate_results),
        "selection_baseline_correct": sum(result.correct for result in baseline_results),
        "human_review": "pending; inspect the exact version and selection report via the local API",
        "final": "not run before an explicit accepted review",
        "full_budget": {
            "development_tasks_baseline_and_candidate": 2 * len(dev_cases),
            "selection_task_runs_candidate_and_fixed": 2 * len(selection),
            "selection_task_runs_heuristic_and_fixed": 2 * len(selection),
            "final_task_runs_candidate_and_fixed": 0,
            "recorded_tool_calls": registry.total_tool_calls(),
            "maximum_total_tool_calls": 200,
            "proposal_model_usage": model_usage,
            "synthetic_tool_cost_usd": 0.0,
        },
        "evidence_note": "Synthetic deterministic results only; no production or cross-family transfer claim.",
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_final(
    db_path: Path,
    version_id: str,
    selection_report_id: str,
    output_path: Path | None,
) -> dict[str, Any]:
    registry = Registry(db_path)
    if not registry.has_accepted_review(version_id, selection_report_id):
        raise ValueError(
            "final evaluation requires an accepted review of this exact selection report"
        )
    if registry.has_finalized_selection(selection_report_id):
        raise ValueError("this sealed selection has already received its one final evaluation")
    proposed, _ = registry.get_version(version_id)
    registry.require_tool_budget(18 * (1 + int(proposed.verify_before_answer) + 1))
    baseline_id = registry.add_version(BASELINE)
    cases = build_split("final")
    report, proposed_results, baseline_results = evaluate_pair(
        version_id,
        proposed,
        baseline_id,
        BASELINE,
        cases,
        split="final",
        seed=SPLIT_SEEDS["final"],
    )
    report = report.model_copy(update={"selection_report_id": selection_report_id})
    registry.add_report(report)
    summary = {
        "generator_version": "synthetic-v1",
        "split": "final",
        "seed": SPLIT_SEEDS["final"],
        "proposed_version_id": version_id,
        "selection_report_id": selection_report_id,
        "report": report.model_dump(mode="json"),
        "proposed_correct": sum(result.correct for result in proposed_results),
        "baseline_correct": sum(result.correct for result in baseline_results),
        "tasks": len(cases),
        "sealed_after_run": True,
        "evidence_note": "One post-freeze synthetic final evaluation; no production claim.",
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded synthetic offline experiment.")
    parser.add_argument("--provider", choices=("fake", "openai-compatible"), default="fake")
    parser.add_argument("--db", type=Path, default=Path("data/runtime.sqlite3"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--finalize-version", default=None)
    parser.add_argument("--selection-report", default=None)
    parser.add_argument(
        "--study",
        choices=("cycle", "history", "final"),
        default=None,
        help="Run the multi-tool v2 protocol in a separate SQLite database.",
    )
    parser.add_argument("--cycle", type=int, default=None)
    parser.add_argument("--report-hash", default=None)
    parser.add_argument("--candidate-hash", default=None)
    parser.add_argument(
        "--auto-reject-ineligible",
        action="store_true",
        help="Record a system rejection for an ineligible study cycle; never auto-approve.",
    )
    args = parser.parse_args()
    if args.study:
        study = Study(args.db)
        if args.study == "cycle":
            result = study.cycle(args.provider)
            if args.auto_reject_ineligible:
                if result["promotion_eligible"]:
                    result["automatic_decision"] = "not rejected: explicit human review required"
                else:
                    result["automatic_decision"] = study.reject_ineligible(result["cycle"])
        elif args.study == "history":
            result = study.history()
        else:
            if args.cycle is None or not args.report_hash or not args.candidate_hash:
                parser.error("study final requires --cycle, --report-hash and --candidate-hash")
            result = study.final(args.cycle, args.report_hash, args.candidate_hash)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    elif args.finalize_version or args.selection_report:
        if not args.finalize_version or not args.selection_report:
            parser.error("--finalize-version and --selection-report must be used together")
        result = run_final(args.db, args.finalize_version, args.selection_report, args.output)
    else:
        result = run_demo(args.provider, args.db, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
