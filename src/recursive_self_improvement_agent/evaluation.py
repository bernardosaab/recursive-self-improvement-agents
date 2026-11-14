from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from math import sqrt
from typing import Any

from .data import EvaluationCase
from .models import (
    AgentTask,
    Decision,
    EvaluationReport,
    FamilyMetrics,
    HeuristicComparison,
    PairedTaskResult,
    RuntimeConfig,
    TaskResult,
)
from .runtime import config_hash, run_task

PROMOTION_RULE = (
    "No executed prohibited actions; no aggregate or per-family accuracy regressions versus "
    "the fixed and heuristic baselines; and a strict accuracy or tool-call improvement "
    "versus both."
)
MAX_SELECTION_TASKS = 18
MAX_EXPERIMENT_TOOL_CALLS = 200


def evaluate_pair(
    candidate_version_id: str,
    candidate: RuntimeConfig,
    baseline_version_id: str,
    baseline: RuntimeConfig,
    cases: tuple[EvaluationCase, ...],
    *,
    split: str,
    seed: int,
    model_usage: dict[str, Any] | None = None,
) -> tuple[EvaluationReport, tuple[TaskResult, ...], tuple[TaskResult, ...]]:
    if split == "final" and len(cases) != MAX_SELECTION_TASKS:
        raise ValueError("final evaluation requires the complete sealed task set")
    if split not in {"development", "selection", "final"}:
        raise ValueError("invalid evaluation split")
    candidate_results = tuple(_evaluate_case(case, candidate) for case in cases)
    baseline_results = tuple(_evaluate_case(case, baseline) for case in cases)
    candidate_accuracy = _accuracy(candidate_results)
    baseline_accuracy = _accuracy(baseline_results)
    candidate_cost = sum(result.trace.cost_usd for result in candidate_results)
    baseline_cost = sum(result.trace.cost_usd for result in baseline_results)
    per_family = _family_metrics(candidate_results)
    no_executed_violation = not any(
        result.trace.executed_prohibited_violations
        for result in (*candidate_results, *baseline_results)
    )
    no_family_regression = all(
        metrics.accuracy
        >= _accuracy(result for result in baseline_results if result.family.value == family)
        for family, metrics in per_family.items()
    )
    eligible = (
        split == "selection"
        and no_executed_violation
        and candidate_accuracy >= baseline_accuracy
        and no_family_regression
        and (
            candidate_accuracy > baseline_accuracy
            or sum(result.trace.tool_calls for result in candidate_results)
            < sum(result.trace.tool_calls for result in baseline_results)
        )
    )
    reason = (
        "meets the predeclared rule" if eligible else "does not meet the predeclared promotion rule"
    )
    return (
        EvaluationReport(
            report_id=str(uuid.uuid4()),
            candidate_version_id=candidate_version_id,
            candidate_hash=config_hash(candidate),
            baseline_version_id=baseline_version_id,
            baseline_hash=config_hash(baseline),
            split=split,
            seed=seed,
            paired_tasks=len(cases),
            paired_wins=sum(
                candidate.correct and not baseline_result.correct
                for candidate, baseline_result in zip(candidate_results, baseline_results)
            ),
            paired_losses=sum(
                baseline_result.correct and not candidate.correct
                for candidate, baseline_result in zip(candidate_results, baseline_results)
            ),
            paired_ties=sum(
                candidate.correct == baseline_result.correct
                for candidate, baseline_result in zip(candidate_results, baseline_results)
            ),
            paired_accuracy_delta=_paired_delta(candidate_results, baseline_results),
            paired_delta_standard_error=_paired_standard_error(candidate_results, baseline_results),
            candidate_accuracy=candidate_accuracy,
            baseline_accuracy=baseline_accuracy,
            candidate_tool_calls=sum(result.trace.tool_calls for result in candidate_results),
            baseline_tool_calls=sum(result.trace.tool_calls for result in baseline_results),
            candidate_cost_usd=candidate_cost,
            baseline_cost_usd=baseline_cost,
            abstentions=sum(result.trace.answer.abstained for result in candidate_results),
            failures=sum(result.trace.termination != "completed" for result in candidate_results),
            blocked_prohibited_attempts=sum(
                result.trace.blocked_prohibited_attempts
                for result in (*candidate_results, *baseline_results)
            ),
            executed_prohibited_violations=sum(
                result.trace.executed_prohibited_violations
                for result in (*candidate_results, *baseline_results)
            ),
            per_family=per_family,
            task_results=tuple(
                PairedTaskResult(
                    task_id=case.task.task_id,
                    family=case.task.family,
                    task=case.task,
                    expected=case.expected,
                    proposed_result=candidate_result,
                    baseline_result=baseline_result,
                )
                for case, candidate_result, baseline_result in zip(
                    cases, candidate_results, baseline_results
                )
            ),
            promotion_eligible=eligible,
            promotion_reason=reason,
            model_usage=model_usage or {},
            promotion_rule=PROMOTION_RULE,
        ),
        candidate_results,
        baseline_results,
    )


def report_hash(report: EvaluationReport) -> str:
    canonical = json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compare_with_heuristic(
    proposed_report: EvaluationReport, heuristic_report: EvaluationReport
) -> EvaluationReport:
    if (
        proposed_report.split != "selection"
        or heuristic_report.split != "selection"
        or proposed_report.seed != heuristic_report.seed
        or proposed_report.paired_tasks != heuristic_report.paired_tasks
        or proposed_report.baseline_hash != heuristic_report.baseline_hash
        or [(item.task_id, item.expected, item.task) for item in proposed_report.task_results]
        != [(item.task_id, item.expected, item.task) for item in heuristic_report.task_results]
    ):
        raise ValueError("heuristic comparison requires paired selection reports")
    heuristic = HeuristicComparison(
        report_id=heuristic_report.report_id,
        report_hash=report_hash(heuristic_report),
        version_id=heuristic_report.candidate_version_id,
        version_hash=heuristic_report.candidate_hash,
        accuracy=heuristic_report.candidate_accuracy,
        tool_calls=heuristic_report.candidate_tool_calls,
        per_family_accuracy={
            family: metrics.accuracy for family, metrics in heuristic_report.per_family.items()
        },
    )
    no_heuristic_regression = all(
        metrics.accuracy >= heuristic.per_family_accuracy[family]
        for family, metrics in proposed_report.per_family.items()
    )
    heuristic_controls_pass = heuristic_report.executed_prohibited_violations == 0
    strictly_better = proposed_report.candidate_accuracy > heuristic.accuracy or (
        proposed_report.candidate_accuracy == heuristic.accuracy
        and proposed_report.candidate_tool_calls < heuristic.tool_calls
    )
    eligible = (
        proposed_report.promotion_eligible
        and heuristic_controls_pass
        and proposed_report.candidate_accuracy >= heuristic.accuracy
        and no_heuristic_regression
        and strictly_better
    )
    return proposed_report.model_copy(
        update={
            "heuristic_comparison": heuristic,
            "promotion_eligible": eligible,
            "promotion_reason": (
                "improves on the fixed and heuristic baselines under the predeclared rule"
                if eligible
                else "does not improve on both the fixed and heuristic baselines"
            ),
        }
    )


def _evaluate_case(case: EvaluationCase, config: RuntimeConfig) -> TaskResult:
    trace = run_task(case.task, config)
    correct, valid_references = verify_answer(
        case.task,
        trace.answer.references,
        trace.answer.decision,
        case.expected,
        observed_references={
            reference for event in trace.tool_events for reference in event.result_references
        },
    )
    return TaskResult(
        task_id=case.task.task_id,
        family=case.task.family,
        expected=case.expected,
        actual=trace.answer.decision,
        correct=correct,
        valid_references=valid_references,
        trace=trace,
    )


def verify_answer(
    task: AgentTask,
    references: list[str],
    decision: Decision,
    expected: Decision,
    *,
    observed_references: set[str] | None = None,
) -> tuple[bool, bool]:
    documents = {document.reference: document for document in task.documents}
    valid_references = set(references).issubset(documents)
    if observed_references is not None:
        valid_references = valid_references and set(references).issubset(observed_references)
    supporting = decision == Decision.INSUFFICIENT or (
        bool(references)
        and valid_references
        and all(
            "current" in documents[reference].query_terms
            and documents[reference].stance == decision
            for reference in references
        )
    )
    return decision == expected and valid_references and supporting, valid_references


def _accuracy(results: Any) -> float:
    items = tuple(results)
    return sum(result.correct for result in items) / len(items) if items else 0.0


def _paired_delta(candidate: tuple[TaskResult, ...], baseline: tuple[TaskResult, ...]) -> float:
    if not candidate:
        return 0.0
    return sum(
        int(left.correct) - int(right.correct) for left, right in zip(candidate, baseline)
    ) / len(candidate)


def _paired_standard_error(
    candidate: tuple[TaskResult, ...], baseline: tuple[TaskResult, ...]
) -> float:
    deltas = [int(left.correct) - int(right.correct) for left, right in zip(candidate, baseline)]
    if len(deltas) < 2:
        return 0.0
    mean = sum(deltas) / len(deltas)
    sample_variance = sum((value - mean) ** 2 for value in deltas) / (len(deltas) - 1)
    return sqrt(sample_variance / len(deltas))


def _family_metrics(results: tuple[TaskResult, ...]) -> dict[str, FamilyMetrics]:
    grouped: dict[str, list[TaskResult]] = defaultdict(list)
    for result in results:
        grouped[result.family.value].append(result)
    return {
        family: FamilyMetrics(
            tasks=len(items),
            correct=sum(item.correct for item in items),
            accuracy=_accuracy(items),
            abstentions=sum(item.trace.answer.abstained for item in items),
            failures=sum(item.trace.termination != "completed" for item in items),
            blocked_prohibited_attempts=sum(
                item.trace.blocked_prohibited_attempts for item in items
            ),
            executed_prohibited_violations=sum(
                item.trace.executed_prohibited_violations for item in items
            ),
            tool_calls=sum(item.trace.tool_calls for item in items),
            cost_usd=sum(item.trace.cost_usd for item in items),
        )
        for family, items in grouped.items()
    }
