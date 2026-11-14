from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from recursive_agent_self_improvement.cli import run_final
from recursive_agent_self_improvement.data import build_split
from recursive_agent_self_improvement.evaluation import (
    compare_with_heuristic,
    evaluate_pair,
    verify_answer,
)
from recursive_agent_self_improvement.improvement import (
    DeterministicFakeProvider,
    apply_proposal,
    validate_proposal,
)
from recursive_agent_self_improvement.models import CandidateProposal, Decision, RuntimeConfig
from recursive_agent_self_improvement.runtime import TrustedToolExecutor, run_task
from recursive_agent_self_improvement.service import make_candidate
from recursive_agent_self_improvement.store import Registry

BASELINE = RuntimeConfig(
    retrieval_strategy="single_pass",
    max_evidence_items=1,
    verify_before_answer=False,
)
REVISED = RuntimeConfig(
    retrieval_strategy="cross_check",
    max_evidence_items=3,
    verify_before_answer=True,
)
HEURISTIC = RuntimeConfig(
    retrieval_strategy="cross_check",
    max_evidence_items=2,
    verify_before_answer=True,
)
CURRENT_FIRST = RuntimeConfig(
    retrieval_strategy="current_first",
    max_evidence_items=2,
    verify_before_answer=False,
)


def test_invalid_reference_is_rejected_by_independent_verifier() -> None:
    task = build_split("development")[0].task

    correct, references_valid = verify_answer(
        task, ["invented-reference"], Decision.SUPPORTED, Decision.SUPPORTED
    )

    assert not correct
    assert not references_valid


def test_v1_verifier_rejects_stale_or_unretrieved_support() -> None:
    case = next(
        case
        for case in build_split("development")
        if case.expected == Decision.SUPPORTED and len(case.task.documents) == 2
    )
    stale = next(doc.reference for doc in case.task.documents if "archive" in doc.query_terms)
    current = next(doc.reference for doc in case.task.documents if "current" in doc.query_terms)
    correct, references_valid = verify_answer(
        case.task, [stale], case.expected, case.expected, observed_references={stale}
    )
    assert references_valid and not correct
    correct, references_valid = verify_answer(
        case.task, [current], case.expected, case.expected, observed_references={stale}
    )
    assert not references_valid and not correct


def test_cross_check_resists_misleading_leading_record() -> None:
    case = next(
        item
        for item in build_split("development")
        if item.expected == Decision.SUPPORTED and item.task.documents
    )

    baseline = run_task(case.task, BASELINE)
    revised = run_task(case.task, REVISED)

    assert baseline.answer.decision == Decision.CONTRADICTED
    assert revised.answer.decision == Decision.SUPPORTED
    assert all(
        reference in {document.reference for document in case.task.documents}
        for reference in revised.answer.references
    )


def test_current_first_reduces_calls_without_losing_verified_outcomes() -> None:
    cases = build_split("selection")
    current_report, current_results, _ = evaluate_pair(
        "current-first",
        CURRENT_FIRST,
        "fixed",
        BASELINE,
        cases,
        split="selection",
        seed=2207,
    )
    heuristic_report, _, _ = evaluate_pair(
        "heuristic",
        HEURISTIC,
        "fixed",
        BASELINE,
        cases,
        split="selection",
        seed=2207,
    )
    compared = compare_with_heuristic(current_report, heuristic_report)

    assert all(result.correct for result in current_results)
    assert current_report.candidate_accuracy == heuristic_report.candidate_accuracy
    assert current_report.candidate_tool_calls < heuristic_report.candidate_tool_calls
    assert compared.promotion_eligible


def test_missing_information_produces_abstention() -> None:
    case = next(
        item for item in build_split("development") if item.expected == Decision.INSUFFICIENT
    )

    trace = run_task(case.task, REVISED)

    assert trace.answer.decision == Decision.INSUFFICIENT
    assert trace.answer.abstained


def test_splits_use_distinct_tasks_and_final_has_extra_edge_evidence() -> None:
    development = build_split("development")
    final = build_split("final")

    assert {case.task.task_id for case in development}.isdisjoint(
        {case.task.task_id for case in final}
    )
    dev_positive = next(case for case in development if case.expected == Decision.SUPPORTED)
    final_positive = next(case for case in final if case.expected == Decision.SUPPORTED)
    assert len(final_positive.task.documents) == len(dev_positive.task.documents) + 1


def test_budget_exhaustion_stops_before_overrun() -> None:
    task = build_split("development")[0].task

    trace = run_task(task, REVISED, max_tool_calls=1)

    assert trace.termination == "budget_exhausted"
    assert trace.tool_calls == 1
    assert trace.tool_events[-1].outcome == "budget_exhausted"


def test_trusted_executor_enforces_fixed_deadline() -> None:
    task = build_split("development")[0].task
    executor = TrustedToolExecutor({task.task_id: task}, deadline_seconds=0)

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        executor.call(task.task_id, "search_evidence", {"query": task.prompt, "limit": 1})
    assert executor.events[-1].outcome == "deadline_exceeded"


def test_prohibited_tool_is_blocked_not_executed() -> None:
    task = build_split("development")[0].task

    trace = run_task(task, BASELINE, prohibited_tool_attempt=True)

    assert trace.blocked_prohibited_attempts == 1
    assert trace.executed_prohibited_violations == 0
    assert trace.tool_events[0].outcome == "prohibited_tool_blocked"


def test_runtime_contract_rejects_permission_and_out_of_bound_changes() -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig(
            retrieval_strategy="cross_check",
            max_evidence_items=99,
            verify_before_answer=True,
        )
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(
            {
                **BASELINE.model_dump(),
                "tool_permissions": ["read_evaluator_labels"],
            }
        )


def test_service_rejects_attempt_to_change_tool_permissions(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.sqlite3")
    root_id = registry.add_version(BASELINE)

    with pytest.raises(ValueError, match="only bounded runtime strategy fields"):
        make_candidate(registry, root_id, {"tool_permissions": ["read_labels"]})

    result = make_candidate(registry, root_id, {"retrieval_strategy": "current_first"})
    stored = {item["version_id"]: item for item in registry.list_versions()}
    assert stored[result["version_id"]]["diff"] == {"retrieval_strategy": "current_first"}


def test_experiment_wide_tool_budget_is_checked_before_execution(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="tool-call budget would be exceeded"):
        registry.require_tool_budget(201)


def test_proposal_must_cite_a_real_development_failure() -> None:
    proposal = CandidateProposal(
        retrieval_strategy="current_first",
        failure_references=["invented-task"],
        rationale="Try a bounded strategy change.",
    )

    with pytest.raises(ValueError, match="absent from development diagnostics"):
        validate_proposal(proposal, [{"task_id": "development-task-01"}])

    no_op = CandidateProposal(
        retrieval_strategy="single_pass",
        failure_references=["development-task-01"],
        rationale="No change.",
    )
    with pytest.raises(ValueError, match="does not change"):
        apply_proposal(BASELINE, no_op)


def test_offline_proposal_strategy_responds_to_trace_evidence() -> None:
    proposal, usage = DeterministicFakeProvider().propose(
        [
            {
                "task_id": "development-task-01",
                "failure": "missed_or_ambiguous_evidence",
                "trace_references": ["development-task-01-decoy"],
            }
        ]
    )

    assert proposal.retrieval_strategy == "current_first"
    assert proposal.failure_references == ["development-task-01"]
    assert usage["provider"] == "deterministic_fake"
    assert usage["input_tokens"] == usage["output_tokens"] == 0


def test_trusted_executor_rejects_out_of_scope_task() -> None:
    task = build_split("development")[0].task
    executor = TrustedToolExecutor({task.task_id: task})

    with pytest.raises(PermissionError, match="outside this runtime"):
        executor.call("another-task", "search_evidence", {"query": task.prompt, "limit": 1})


def test_review_is_bound_to_exact_report_and_rollback_target(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.sqlite3")
    root_id = registry.add_version(BASELINE)
    registry.register_initial(root_id)
    proposed_id = registry.add_version(REVISED, root_id)
    report, _, _ = evaluate_pair(
        proposed_id,
        REVISED,
        root_id,
        BASELINE,
        build_split("selection"),
        split="selection",
        seed=2207,
    )
    registry.add_report(report)

    with pytest.raises(ValueError, match="does not evaluate this exact"):
        registry.decide(root_id, report.report_id, "local-reviewer", True, "wrong target")
    with pytest.raises(ValueError, match="requires an accepted review"):
        run_final(tmp_path / "registry.sqlite3", proposed_id, report.report_id, None)

    decision = registry.decide(
        proposed_id, report.report_id, "local-reviewer", True, "reviewed report"
    )
    assert decision.accepted
    assert registry.active_version() == proposed_id
    assert registry.has_accepted_review(proposed_id, report.report_id)

    final_report = run_final(tmp_path / "registry.sqlite3", proposed_id, report.report_id, None)
    assert final_report["split"] == "final"
    assert final_report["tasks"] == 18
    with pytest.raises(ValueError, match="already received"):
        run_final(tmp_path / "registry.sqlite3", proposed_id, report.report_id, None)

    result = registry.rollback(root_id, "local-reviewer", "restore fixed runtime")
    assert result["active_version_id"] == root_id
    assert registry.active_version() == root_id
    assert result["effect"] == "configuration_pointer_only"


def test_report_tampering_invalidates_review(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    root_id = registry.add_version(BASELINE)
    proposed_id = registry.add_version(REVISED, root_id)
    report, _, _ = evaluate_pair(
        proposed_id,
        REVISED,
        root_id,
        BASELINE,
        build_split("selection"),
        split="selection",
        seed=2207,
    )
    registry.add_report(report)
    with sqlite3.connect(path) as connection:
        payload = json.loads(report.model_dump_json())
        payload["paired_tasks"] += 1
        connection.execute(
            "UPDATE reports SET report_json = ? WHERE report_id = ?",
            (json.dumps(payload), report.report_id),
        )

    with pytest.raises(ValueError, match="stored report hash mismatch"):
        registry.decide(proposed_id, report.report_id, "local-reviewer", True, "tampered")


def test_version_tampering_invalidates_review(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    root_id = registry.add_version(BASELINE)
    proposed_id = registry.add_version(CURRENT_FIRST, root_id)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE versions SET config_json = ? WHERE version_id = ?",
            (HEURISTIC.model_dump_json(), proposed_id),
        )

    with pytest.raises(ValueError, match="stored version hash mismatch"):
        registry.get_version(proposed_id)
