from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskFamily(StrEnum):
    EVIDENCE_RECONCILIATION = "evidence_reconciliation"
    CUSTOMER_CASE = "customer_case"
    OPERATIONAL_TRIAGE = "operational_triage"


class Decision(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"


class EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    content: str
    stance: Decision | None
    query_terms: list[str]


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    prompt: str
    documents: tuple[EvidenceDocument, ...]


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retrieval_strategy: str = Field(pattern=r"^(single_pass|cross_check|current_first)$")
    max_evidence_items: int = Field(ge=1, le=3)
    verify_before_answer: bool


class CandidateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retrieval_strategy: str | None = Field(
        default=None, pattern=r"^(single_pass|cross_check|current_first)$"
    )
    max_evidence_items: int | None = Field(default=None, ge=1, le=3)
    verify_before_answer: bool | None = None
    failure_references: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_a_change(self) -> CandidateProposal:
        if not any(
            value is not None
            for value in (
                self.retrieval_strategy,
                self.max_evidence_items,
                self.verify_before_answer,
            )
        ):
            raise ValueError("proposal must change at least one bounded runtime strategy")
        return self

    def changes(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.model_dump(
                exclude={"failure_references", "rationale"}, exclude_none=True
            ).items()
        }


class ToolEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    arguments: dict[str, Any]
    outcome: str
    blocked: bool = False
    result_references: list[str] = Field(default_factory=list)


class AgentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    references: list[str]
    abstained: bool


class RunTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    config_hash: str
    tool_events: tuple[ToolEvent, ...]
    answer: AgentAnswer
    termination: str
    tool_calls: int
    blocked_prohibited_attempts: int
    executed_prohibited_violations: int
    cost_usd: float


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    expected: Decision
    actual: Decision
    correct: bool
    valid_references: bool
    trace: RunTrace


class FamilyMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: int
    correct: int
    accuracy: float
    abstentions: int
    failures: int
    blocked_prohibited_attempts: int
    executed_prohibited_violations: int
    tool_calls: int
    cost_usd: float


class HeuristicComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    report_hash: str
    version_id: str
    version_hash: str
    accuracy: float
    tool_calls: int
    per_family_accuracy: dict[str, float]


class PairedTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    family: TaskFamily
    task: AgentTask
    expected: Decision
    proposed_result: TaskResult
    baseline_result: TaskResult


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    candidate_version_id: str
    candidate_hash: str
    baseline_version_id: str
    baseline_hash: str
    selection_report_id: str | None = None
    heuristic_comparison: HeuristicComparison | None = None
    split: str
    seed: int
    paired_tasks: int
    paired_wins: int
    paired_losses: int
    paired_ties: int
    paired_accuracy_delta: float
    paired_delta_standard_error: float
    candidate_accuracy: float
    baseline_accuracy: float
    candidate_tool_calls: int
    baseline_tool_calls: int
    candidate_cost_usd: float
    baseline_cost_usd: float
    abstentions: int
    failures: int
    blocked_prohibited_attempts: int
    executed_prohibited_violations: int
    per_family: dict[str, FamilyMetrics]
    task_results: tuple[PairedTaskResult, ...]
    promotion_eligible: bool
    promotion_reason: str
    model_usage: dict[str, Any]
    promotion_rule: str


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    candidate_version_id: str
    candidate_hash: str
    report_id: str
    report_hash: str
    reviewer: str = Field(min_length=1, max_length=120)
    accepted: bool
    reason: str = Field(min_length=1, max_length=1000)
    created_at: str
