"""Known-path compiler: promote recurring work to a zero-model deterministic recipe.

A recipe is proposed for a family only when, on development tasks, the deterministic
path (a) applies to at least ``min_coverage`` of tasks and (b) matches the verified
outcome on every task it covers. At runtime it *skips* (with a recorded reason) to the
agentic path whenever its preconditions fail, e.g. schema drift or conflicting
records; it never guesses.

Coupling disclosure: the recipe implements the documented resolution rule that also
defines the synthetic labels, so its accuracy on covered tasks is by construction.
What is measured is coverage, skip behaviour under drift and the cost it removes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .harness import (
    EDIT_OP_ADAPTER,
    Family,
    HarnessConfig,
    HarnessProposal,
    harness_hash,
)
from .runtime import TrustedExecutor, compiled_answer
from .tasks import Case


def mine(cases: list[Case], min_coverage: float = 0.5) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for family in sorted({c.task.family for c in cases}, key=lambda f: f.value):
        members = [c for c in cases if c.task.family == family]
        covered = correct = 0
        skips: Counter[str] = Counter()
        for case in members:
            executor = TrustedExecutor(
                case.task, case.env.model_copy(update={"fault_first_call": False})
            )
            raw = executor.call(executor.read_tool, {"entity": case.task.claim.entity})
            answer, skip = compiled_answer(case.task, raw)
            if answer is None:
                skips[skip or "unknown"] += 1
                continue
            covered += 1
            refs_ok = set(answer["references"]) == set(case.supporting)
            write = [a["arguments"]["amount"] for a in answer["actions"]]
            write_ok = write == ([case.expected_write] if case.expected_write is not None else [])
            correct += int(answer["decision"] == case.expected and refs_ok and write_ok)
        coverage = covered / len(members) if members else 0.0
        report[family.value] = {
            "tasks": len(members),
            "covered": covered,
            "coverage": coverage,
            "covered_correct": correct,
            "skip_reasons": dict(skips),
            "eligible": covered > 0 and correct == covered and coverage >= min_coverage,
        }
    return report


def propose(
    parent: HarnessConfig, cases: list[Case], min_coverage: float = 0.5
) -> tuple[HarnessProposal | None, dict[str, Any]]:
    report = mine(cases, min_coverage)
    families = [
        Family(name)
        for name, row in report.items()
        if row["eligible"]
        and parent.recipe_for(Family(name)) is None
        and Family(name) != Family.CONTRACT
    ]
    if not families:
        return None, report
    proposal = HarnessProposal(
        parent_hash=harness_hash(parent),
        ops=tuple(
            EDIT_OP_ADAPTER.validate_python({"op": "add_compiled", "family": f}) for f in families
        ),
        cited_failures=tuple(f"compile:{f.value}" for f in families),
        rationale="Development traces show a stable known path with full agreement on covered tasks.",
        source="known_path_compiler",
    )
    return proposal, report
