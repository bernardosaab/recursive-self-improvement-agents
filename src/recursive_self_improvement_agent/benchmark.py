"""Synthetic, multi-tool benchmark with evaluator-only reference requirements."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import Decision, RuntimeConfig, TaskFamily

GENERATOR_VERSION = "multi-tool-v2"
SPLIT_SEEDS = {"development": 4103, "selection": 4207, "final": 4301}
TOOLS = {
    TaskFamily.EVIDENCE_RECONCILIATION: "search_documents",
    TaskFamily.CUSTOMER_CASE: "read_case_records",
    TaskFamily.OPERATIONAL_TRIAGE: "read_telemetry",
}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    kind: str
    valid_from: int
    valid_until: int
    fields: dict[str, Any]


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    as_of: int
    claim: str
    records: tuple[Record, ...]


@dataclass(frozen=True)
class BenchmarkCase:
    task: BenchmarkTask
    expected: Decision
    supporting_references: frozenset[str]


class BenchmarkTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    events: tuple[dict[str, Any], ...]
    decision: Decision
    references: tuple[str, ...]
    termination: str
    tool_calls: int
    blocked_attempts: int
    executed_violations: int


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace: BenchmarkTrace
    expected: Decision
    correct: bool
    valid_references: bool
    supported_references: bool


class Budget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise RuntimeError("experiment tool-call budget exhausted")
        self.used += 1


class ScopedExecutor:
    """Only the current task's family-specific read tool is accessible."""

    def __init__(self, task: BenchmarkTask, budget: Budget, max_calls: int = 3) -> None:
        self.task = task
        self.budget = budget
        self.max_calls = max_calls
        self.deadline = time.monotonic() + 5.0
        self.events: list[dict[str, Any]] = []
        self.calls = 0

    def call(self, task_id: str, tool: str, mode: str, limit: int) -> tuple[Record, ...]:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("runtime deadline exceeded")
        if tool != TOOLS[self.task.family] or task_id != self.task.task_id:
            self.events.append({"tool": tool, "outcome": "blocked", "references": []})
            raise PermissionError("tool or task is outside scope")
        if mode not in {"all", "as_of"} or type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError("invalid tool arguments")
        if self.calls >= self.max_calls:
            raise RuntimeError("per-task tool-call budget exhausted")
        self.budget.consume()
        self.calls += 1
        records = self.task.records
        if mode == "as_of":
            records = tuple(
                record
                for record in records
                if record.valid_from <= self.task.as_of < record.valid_until
            )
        selected = records[:limit]
        self.events.append(
            {
                "tool": tool,
                "mode": mode,
                "outcome": "ok",
                "references": [record.reference for record in selected],
            }
        )
        return selected


def build_cases(split: str, seed: int | None = None) -> tuple[BenchmarkCase, ...]:
    if split not in SPLIT_SEEDS:
        raise ValueError(f"unknown split: {split}")
    rng = random.Random(SPLIT_SEEDS[split] if seed is None else seed)
    cases: list[BenchmarkCase] = []
    for family in TaskFamily:
        for index in range(9):
            task_id = f"{split}-{family.value}-{index:02d}"
            as_of = 100
            # The final split exercises an inclusive validity boundary.
            current_start = 100 if split == "final" and index % 2 == 0 else 80
            end_old = current_start
            expected_kind = index % 3
            refs = [f"{task_id}-{value:04x}" for value in rng.sample(range(4096, 65535), 4)]
            if family == TaskFamily.EVIDENCE_RECONCILIATION:
                claimed = 100 + index
                actual = claimed if expected_kind == 0 else claimed + 7
                records = [
                    Record(
                        reference=refs[0],
                        kind="invoice",
                        valid_from=0,
                        valid_until=end_old,
                        fields={"amount": claimed if expected_kind == 1 else claimed + 7},
                    ),
                    Record(
                        reference=refs[1],
                        kind="invoice",
                        valid_from=current_start,
                        valid_until=200,
                        fields={"amount": actual},
                    ),
                ]
                if expected_kind == 2:
                    records.pop()
                claim = f"invoice amount is {claimed}"
                supporting = frozenset({refs[1]}) if expected_kind != 2 else frozenset()
            elif family == TaskFamily.CUSTOMER_CASE:
                plan = "standard" if expected_kind != 1 else "basic"
                records = [
                    Record(
                        reference=refs[0],
                        kind="policy",
                        valid_from=0,
                        valid_until=end_old,
                        fields={"plans": ["basic"], "max_age": 90},
                    ),
                    Record(
                        reference=refs[1],
                        kind="account",
                        valid_from=0,
                        valid_until=200,
                        fields={"plan": plan, "purchase_day": 90},
                    ),
                    Record(
                        reference=refs[2],
                        kind="policy",
                        valid_from=current_start,
                        valid_until=200,
                        fields={"plans": ["standard"], "max_age": 30},
                    ),
                ]
                if expected_kind == 2:
                    records.pop()
                claim = "customer is eligible for a replacement"
                supporting = frozenset({refs[1], refs[2]}) if expected_kind != 2 else frozenset()
            else:
                errors = 8 if expected_kind != 1 else 2
                records = [
                    Record(
                        reference=refs[0],
                        kind="metric",
                        valid_from=0,
                        valid_until=end_old,
                        fields={"errors": 9 if expected_kind == 1 else 1},
                    ),
                    Record(
                        reference=refs[1],
                        kind="metric",
                        valid_from=current_start,
                        valid_until=200,
                        fields={"errors": errors},
                    ),
                    Record(
                        reference=refs[2],
                        kind="threshold",
                        valid_from=0,
                        valid_until=200,
                        fields={"errors_above": 5},
                    ),
                ]
                if expected_kind == 2:
                    records.pop()
                claim = "the error signal exceeds the triage threshold"
                supporting = frozenset({refs[1], refs[2]}) if expected_kind != 2 else frozenset()
            if index >= 6 and expected_kind != 2:
                # Conflicting observations are visible, but marked as unverified sources.
                decoys = (
                    3
                    if split == "final"
                    else 2
                    if family == TaskFamily.EVIDENCE_RECONCILIATION
                    else 1
                )
                misleading = [
                    Record(
                        reference=f"{task_id}-{value:04x}",
                        kind=records[1].kind,
                        valid_from=current_start,
                        valid_until=200,
                        fields={**records[1].fields, "source_status": "unverified"},
                    )
                    for value in rng.sample(range(65536, 99999), decoys)
                ]
                records = [records[0], *misleading, *records[1:]]
            expected = (Decision.SUPPORTED, Decision.CONTRADICTED, Decision.INSUFFICIENT)[
                expected_kind
            ]
            cases.append(
                BenchmarkCase(
                    task=BenchmarkTask(
                        task_id=task_id,
                        family=family,
                        as_of=as_of,
                        claim=claim,
                        records=tuple(records),
                    ),
                    expected=expected,
                    supporting_references=supporting,
                )
            )
    return tuple(cases)


def run_case(
    case: BenchmarkCase,
    config: RuntimeConfig,
    budget: Budget,
    *,
    prohibited_attempt: bool = False,
) -> BenchmarkResult:
    task = case.task
    executor = ScopedExecutor(task, budget)
    if prohibited_attempt:
        try:
            executor.call(task.task_id, "read_evaluator_labels", "all", 1)
        except PermissionError:
            pass
    mode = "all" if config.retrieval_strategy == "single_pass" else "as_of"
    limit = 1 if config.retrieval_strategy == "single_pass" else config.max_evidence_items
    termination = "completed"
    try:
        records = executor.call(task.task_id, TOOLS[task.family], mode, limit)
        if config.verify_before_answer:
            records = executor.call(
                task.task_id, TOOLS[task.family], "as_of", config.max_evidence_items
            )
        decision, references = _resolve(task, records)
    except (RuntimeError, TimeoutError) as error:
        decision, references = Decision.INSUFFICIENT, ()
        termination = str(error)
    trace = BenchmarkTrace(
        task_id=task.task_id,
        family=task.family,
        events=tuple(executor.events),
        decision=decision,
        references=references,
        termination=termination,
        tool_calls=executor.calls,
        blocked_attempts=sum(event["outcome"] == "blocked" for event in executor.events),
        executed_violations=0,
    )
    return verify(case, trace)


def verify(case: BenchmarkCase, trace: BenchmarkTrace) -> BenchmarkResult:
    all_references = {record.reference for record in case.task.records}
    observed = {reference for event in trace.events for reference in event.get("references", [])}
    valid = set(trace.references).issubset(all_references & observed)
    supported = trace.decision == Decision.INSUFFICIENT or (
        set(trace.references) == case.supporting_references and bool(case.supporting_references)
    )
    return BenchmarkResult(
        trace=trace,
        expected=case.expected,
        correct=(
            trace.termination == "completed"
            and trace.decision == case.expected
            and valid
            and supported
        ),
        valid_references=valid,
        supported_references=supported,
    )


def _resolve(task: BenchmarkTask, records: tuple[Record, ...]) -> tuple[Decision, tuple[str, ...]]:
    by_kind: dict[str, Record] = {}
    for record in records:
        if record.fields.get("source_status") == "unverified":
            continue
        if record.kind not in by_kind or record.valid_from > by_kind[record.kind].valid_from:
            by_kind[record.kind] = record
    if task.family == TaskFamily.EVIDENCE_RECONCILIATION:
        record = by_kind.get("invoice")
        if record is None:
            return Decision.INSUFFICIENT, ()
        claimed = int(task.claim.rsplit(" ", 1)[1])
        supported = record.fields["amount"] == claimed
        used = (record.reference,)
    elif task.family == TaskFamily.CUSTOMER_CASE:
        account, policy = by_kind.get("account"), by_kind.get("policy")
        if account is None or policy is None:
            return Decision.INSUFFICIENT, ()
        supported = (
            account.fields["plan"] in policy.fields["plans"]
            and task.as_of - account.fields["purchase_day"] <= policy.fields["max_age"]
        )
        used = (account.reference, policy.reference)
    else:
        metric, threshold = by_kind.get("metric"), by_kind.get("threshold")
        if metric is None or threshold is None:
            return Decision.INSUFFICIENT, ()
        supported = metric.fields["errors"] > threshold.fields["errors_above"]
        used = (metric.reference, threshold.reference)
    return (Decision.SUPPORTED if supported else Decision.CONTRADICTED), used
