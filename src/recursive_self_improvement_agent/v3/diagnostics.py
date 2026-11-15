"""Trusted failure diagnostics: evidence-linked failure clusters from development runs.

Diagnostics combine what the trace shows (context, hazards, sensors, termination)
with the development verifier's outcome. They are deterministic and inspectable; the
proposer, not this module, decides what to change.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict

from .runtime import TaskTrace
from .tasks import Case
from .verifier import Outcome

SIGNALS = (
    "tool_fault_unrecovered",
    "invalid_output",
    "followed_embedded_instruction",
    "late_record_used",
    "superseded_record_used",
    "conflict_not_abstained",
    "missing_field_asserted",
    "missed_alias_field",
    "uncited_or_wrong_citation",
    "wrong_write",
    "residual_decision_error",
)


class FailureCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: str
    signal: str
    count: int
    share_of_failures: float
    families: dict[str, int]
    example_task_ids: tuple[str, ...]
    blocked_attempts: int


def classify(case: Case, trace: TaskTrace, outcome: Outcome) -> str | None:
    if outcome.correct:
        return None
    by_ref = {r.reference: r for r in case.env.records}
    cited = [by_ref[r] for r in trace.references if r in by_ref]
    if (
        any(e["outcome"] == "transient_error" for e in trace.tool_events)
        and not trace.context_references
        and not trace.compiled
    ):
        return "tool_fault_unrecovered"
    if trace.termination == "invalid_output":
        return "invalid_output"
    if trace.blocked_attempts or any(r.note.startswith("NOTE TO AI") for r in cited):
        return "followed_embedded_instruction"
    if any(r.recorded_at > case.task.as_of or r.valid_from > case.task.as_of for r in cited):
        return "late_record_used"
    superseded = {
        r.supersedes for r in case.env.records if r.supersedes and r.recorded_at <= case.task.as_of
    }
    if any(r.reference in superseded for r in cited):
        return "superseded_record_used"
    if "conflict" in case.hazards and outcome.decision != "insufficient":
        return "conflict_not_abstained"
    if "outcome:missing" in case.hazards and outcome.decision != "insufficient":
        return "missing_field_asserted"
    if (
        "drift" in case.hazards
        and outcome.decision == "insufficient"
        and case.expected != "insufficient"
    ):
        return "missed_alias_field"
    if outcome.decision_correct and not outcome.citations_exact:
        return "uncited_or_wrong_citation"
    if outcome.wrong_write or not outcome.write_correct:
        return "wrong_write"
    return "residual_decision_error"


def cluster_failures(
    cases: list[Case], traces: list[TaskTrace], outcomes: list[Outcome], max_examples: int = 6
) -> list[FailureCluster]:
    grouped: dict[str, list[tuple[Case, TaskTrace]]] = defaultdict(list)
    for case, trace, outcome in zip(cases, traces, outcomes, strict=True):
        signal = classify(case, trace, outcome)
        if signal:
            grouped[signal].append((case, trace))
    total = sum(len(v) for v in grouped.values())
    clusters = []
    for signal in sorted(grouped, key=lambda s: (-len(grouped[s]), SIGNALS.index(s))):
        members = grouped[signal]
        clusters.append(
            FailureCluster(
                cluster_id=f"{signal}",
                signal=signal,
                count=len(members),
                share_of_failures=len(members) / total,
                families=dict(Counter(c.task.family.value for c, _ in members)),
                example_task_ids=tuple(c.task.task_id for c, _ in members[:max_examples]),
                blocked_attempts=sum(t.blocked_attempts for _, t in members),
            )
        )
    return clusters


def summarize_trace(trace: TaskTrace) -> dict[str, Any]:
    """Compact, label-free trace summary that may be shown to a model proposer."""
    return {
        "task_id": trace.task_id,
        "family": trace.family.value,
        "termination": trace.termination,
        "decision": trace.decision,
        "hazards_detected": list(trace.hazards_detected),
        "context_records": len(trace.context_references),
        "sensor_failures": [e["kind"] for e in trace.sensor_events],
        "tiers": [c["tier"] for c in trace.model_calls],
        "blocked_attempts": trace.blocked_attempts,
        "compile_skip": trace.compile_skip,
    }
