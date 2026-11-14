from __future__ import annotations

import random
from dataclasses import dataclass

from .models import AgentTask, Decision, EvidenceDocument, TaskFamily

GENERATOR_VERSION = "synthetic-v1"
SPLIT_SEEDS = {"development": 1103, "selection": 2207, "final": 3301}
FAMILIES = tuple(TaskFamily)


@dataclass(frozen=True)
class EvaluationCase:
    task: AgentTask
    expected: Decision


def build_split(split: str, seed: int | None = None) -> tuple[EvaluationCase, ...]:
    if split not in SPLIT_SEEDS:
        raise ValueError(f"unknown split: {split}")
    rng = random.Random(SPLIT_SEEDS[split] if seed is None else seed)
    cases: list[EvaluationCase] = []
    for family in FAMILIES:
        case_numbers = rng.sample(range(100, 999), 6)
        for index, case_number in enumerate(case_numbers):
            expected = (Decision.SUPPORTED, Decision.CONTRADICTED, Decision.INSUFFICIENT)[index % 3]
            task_id = f"{split}-{family.value}-{case_number:03d}"
            query = _query_for(family, case_number)
            documents: list[EvidenceDocument] = []
            if expected != Decision.INSUFFICIENT:
                # The leading distractor deliberately conflicts with the answer.
                distractor = (
                    Decision.CONTRADICTED if expected == Decision.SUPPORTED else Decision.SUPPORTED
                )
                documents.append(
                    EvidenceDocument(
                        reference=f"{task_id}-decoy",
                        content=f"Outdated or misleading record for {query}.",
                        stance=distractor,
                        query_terms=[query, "archive"],
                    )
                )
                documents.append(
                    EvidenceDocument(
                        reference=f"{task_id}-source",
                        content=f"Current source record: {query} is {expected.value}.",
                        stance=expected,
                        query_terms=[query, "current"],
                    )
                )
                if split == "final":
                    documents.append(
                        EvidenceDocument(
                            reference=f"{task_id}-second-archive",
                            content=f"Another stale observation conflicts with {query}.",
                            stance=distractor,
                            query_terms=[query, "archive"],
                        )
                    )
            else:
                partial_count = 2 if split == "final" else int(index % 2 == 1)
                for partial_index in range(partial_count):
                    documents.append(
                        EvidenceDocument(
                            reference=f"{task_id}-partial-{partial_index}",
                            content=f"Partial record for {query}; decisive information is absent.",
                            stance=None,
                            query_terms=[query],
                        )
                    )
            rng.shuffle(documents)
            cases.append(
                EvaluationCase(
                    task=AgentTask(
                        task_id=task_id,
                        family=family,
                        prompt=f"Determine whether the available evidence supports: {query}",
                        documents=tuple(documents),
                    ),
                    expected=expected,
                )
            )
    return tuple(cases)


def _query_for(family: TaskFamily, index: int) -> str:
    subject = {
        TaskFamily.EVIDENCE_RECONCILIATION: "invoice total",
        TaskFamily.CUSTOMER_CASE: "replacement eligibility",
        TaskFamily.OPERATIONAL_TRIAGE: "service health signal",
    }[family]
    return f"{subject} case {index:03d}"
