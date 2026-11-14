"""Bounded local study; one human-reviewed proposal per cycle."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .benchmark import (
    GENERATOR_VERSION,
    SPLIT_SEEDS,
    BenchmarkCase,
    BenchmarkResult,
    Budget,
    build_cases,
    run_case,
)
from .improvement import OpenAICompatibleProvider, validate_proposal
from .models import CandidateProposal, RuntimeConfig, TaskFamily
from .runtime import config_hash

MAX_CYCLES = 3
MAX_TOOL_CALLS = 1500
MAX_PROPOSAL_CALLS = 3
PROMOTION_RULE = (
    "No executed violations or incorrect family regressions versus the active version, "
    "strong fixed, verification heuristic, or equal-attempt search; strictly improve "
    "accuracy or tool calls versus the active version and strong fixed."
)
INITIAL = RuntimeConfig(
    retrieval_strategy="current_first", max_evidence_items=2, verify_before_answer=False
)
FIXED = RuntimeConfig(
    retrieval_strategy="current_first", max_evidence_items=3, verify_before_answer=False
)
HEURISTIC = RuntimeConfig(
    retrieval_strategy="cross_check", max_evidence_items=3, verify_before_answer=True
)
SEARCH_SPACE = (
    RuntimeConfig(
        retrieval_strategy="single_pass", max_evidence_items=1, verify_before_answer=False
    ),
    RuntimeConfig(
        retrieval_strategy="cross_check", max_evidence_items=3, verify_before_answer=False
    ),
    RuntimeConfig(
        retrieval_strategy="current_first", max_evidence_items=3, verify_before_answer=False
    ),
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class Study:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS study_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS study_cycles (
                    cycle INTEGER PRIMARY KEY,
                    report_json TEXT NOT NULL,
                    report_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS study_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    cycle INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    reserved_tool_calls INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS study_decisions (
                    decision_id TEXT PRIMARY KEY,
                    cycle INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS study_final (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    result_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS study_rollbacks (
                    rollback_id TEXT PRIMARY KEY,
                    candidate_hash TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT
                );
                """
            )
            for table in ("study_decisions", "study_rollbacks"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "created_at" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN created_at TEXT")
            for key, value in {
                "active_config": _json(INITIAL.model_dump(mode="json")),
                "tool_calls_reserved": "0",
                "proposal_calls": "0",
                "rule": PROMOTION_RULE,
                "final_opened": "0",
            }.items():
                db.execute("INSERT OR IGNORE INTO study_state VALUES (?, ?)", (key, value))
            stored_rule = db.execute("SELECT value FROM study_state WHERE key = 'rule'").fetchone()[
                0
            ]
            if stored_rule != PROMOTION_RULE:
                raise ValueError("stored study rule does not match this protocol")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _state(self, key: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM study_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return str(row[0])

    def _reserve(self, calls: int, proposal: bool = False) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = int(
                db.execute(
                    "SELECT value FROM study_state WHERE key = 'tool_calls_reserved'"
                ).fetchone()[0]
            )
            proposals = int(
                db.execute("SELECT value FROM study_state WHERE key = 'proposal_calls'").fetchone()[
                    0
                ]
            )
            if used + calls > MAX_TOOL_CALLS or proposals + int(proposal) > MAX_PROPOSAL_CALLS:
                raise ValueError("study budget exhausted")
            db.execute(
                "UPDATE study_state SET value = ? WHERE key = 'tool_calls_reserved'",
                (str(used + calls),),
            )
            if proposal:
                db.execute(
                    "UPDATE study_state SET value = ? WHERE key = 'proposal_calls'",
                    (str(proposals + 1),),
                )

    def _reconcile(self, reserved: int, actual: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = int(
                db.execute(
                    "SELECT value FROM study_state WHERE key = 'tool_calls_reserved'"
                ).fetchone()[0]
            )
            db.execute(
                "UPDATE study_state SET value = ? WHERE key = 'tool_calls_reserved'",
                (str(used - reserved + actual),),
            )

    def cycle(self, provider: str = "fake") -> dict[str, Any]:
        if provider not in {"fake", "openai-compatible"}:
            raise ValueError("unknown proposal provider")
        with self.connect() as db:
            rows = db.execute("SELECT cycle FROM study_cycles ORDER BY cycle").fetchall()
            final_opened = db.execute(
                "SELECT value FROM study_state WHERE key = 'final_opened'"
            ).fetchone()[0]
            if final_opened == "1":
                raise ValueError("the final test has already been opened")
            cycle = len(rows) + 1
            if cycle > MAX_CYCLES:
                raise ValueError("maximum selection cycles reached")
            if (
                rows
                and not db.execute(
                    "SELECT 1 FROM study_decisions WHERE cycle = ?", (cycle - 1,)
                ).fetchone()
            ):
                raise ValueError("review the previous cycle before starting another")
        # Reserve worst case before any call. A crash retains the reservation, not a free retry.
        reservation = 27 * (2 * 2 + 5 * 2) + 6
        self._reserve(reservation, proposal=True)
        attempt_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO study_attempts VALUES (?, ?, ?, ?, 'started')",
                (attempt_id, cycle, provider, reservation),
            )
        budget = Budget(reservation)
        parent = RuntimeConfig.model_validate_json(self._state("active_config"))
        dev = build_cases("development", SPLIT_SEEDS["development"] + cycle - 1)
        parent_dev = _run(dev, parent, budget)
        diagnostics = _diagnostics(dev, parent_dev)
        if not diagnostics:
            raise ValueError("no verified development failures; no proposal generated")
        if provider == "fake":
            proposal, usage = _fake_proposal(diagnostics, parent, cycle)
        else:
            proposal, usage = OpenAICompatibleProvider.from_environment().propose(diagnostics)
        validate_proposal(proposal, diagnostics)
        candidate = RuntimeConfig.model_validate({**parent.model_dump(), **proposal.changes()})
        search = random.Random(7000 + cycle).choice(SEARCH_SPACE)
        search_dev = _run(dev, search, budget)
        selection = build_cases("selection", SPLIT_SEEDS["selection"])
        methods = {
            "proposed": candidate,
            "active": parent,
            "strong_fixed": FIXED,
            "heuristic": HEURISTIC,
            "non_trace_search": search,
        }
        results = {name: _run(selection, config, budget) for name, config in methods.items()}
        metrics = {name: _metrics(items) for name, items in results.items()}
        adversarial_cases = tuple(
            next(case for case in selection if case.task.family == family) for family in TaskFamily
        )
        adversarial = [
            run_case(case, candidate, budget, prohibited_attempt=True) for case in adversarial_cases
        ]
        adversarial_metrics = _metrics(adversarial)
        eligible = (
            _eligible(metrics, candidate != parent)
            and adversarial_metrics["executed_violations"] == 0
        )
        comparison = {
            name: _paired(results["proposed"], items)
            for name, items in results.items()
            if name != "proposed"
        }
        report: dict[str, Any] = {
            "protocol": "bounded-study-v2",
            "generator_version": GENERATOR_VERSION,
            "promotion_rule": PROMOTION_RULE,
            "cycle": cycle,
            "development_seed": SPLIT_SEEDS["development"] + cycle - 1,
            "selection_seed": SPLIT_SEEDS["selection"],
            "parent": parent.model_dump(mode="json"),
            "parent_hash": config_hash(parent),
            "candidate": candidate.model_dump(mode="json"),
            "candidate_hash": config_hash(candidate),
            "candidate_diff": {
                key: value
                for key, value in candidate.model_dump(mode="json").items()
                if value != parent.model_dump(mode="json")[key]
            },
            "proposal": proposal.model_dump(mode="json"),
            "diagnostics": diagnostics,
            "provider_usage": usage,
            "development": {
                "trace_method": _metrics(parent_dev),
                "non_trace_search": _metrics(search_dev),
                "task_runs_per_method": len(dev),
            },
            "selection": {
                "methods": {
                    name: {"config": methods[name].model_dump(mode="json"), **metrics[name]}
                    for name in methods
                },
                "paired_vs_proposed": comparison,
                "task_results": {
                    name: [item.model_dump(mode="json") for item in items]
                    for name, items in results.items()
                },
            },
            "promotion_eligible": eligible,
            "adversarial": {
                "metrics": adversarial_metrics,
                "results": [item.model_dump(mode="json") for item in adversarial],
            },
            "status": "awaiting_review",
            "budget": {
                "tool_calls_this_cycle": budget.used,
                "proposal_calls_this_cycle": 1,
                "max_tool_calls": MAX_TOOL_CALLS,
                "max_proposal_calls": MAX_PROPOSAL_CALLS,
            },
        }
        digest = _hash(report)
        with self.connect() as db:
            db.execute("INSERT INTO study_cycles VALUES (?, ?, ?)", (cycle, _json(report), digest))
            db.execute(
                "UPDATE study_attempts SET status = 'completed' WHERE attempt_id = ?",
                (attempt_id,),
            )
        self._reconcile(reservation, budget.used)
        return {**report, "report_hash": digest}

    def report(self, cycle: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT report_json, report_hash FROM study_cycles WHERE cycle = ?", (cycle,)
            ).fetchone()
        if row is None:
            raise KeyError(cycle)
        report = json.loads(row[0])
        if _hash(report) != row[1]:
            raise ValueError("study report hash mismatch")
        if (
            config_hash(RuntimeConfig.model_validate(report["candidate"]))
            != report["candidate_hash"]
        ):
            raise ValueError("study candidate hash mismatch")
        return {**report, "report_hash": row[1]}

    def review(
        self,
        cycle: int,
        report_hash: str,
        candidate_hash: str,
        reviewer: str,
        accepted: bool,
        reason: str,
    ) -> dict[str, Any]:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        report = self.report(cycle)
        if report["report_hash"] != report_hash or report["candidate_hash"] != candidate_hash:
            raise ValueError("review does not match the exact report and candidate")
        if accepted and not report["promotion_eligible"]:
            raise ValueError("proposal does not satisfy the fixed promotion rule")
        decision = {
            "decision_id": str(uuid.uuid4()),
            "cycle": cycle,
            "candidate_hash": candidate_hash,
            "report_hash": report_hash,
            "reviewer": reviewer,
            "accepted": accepted,
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM study_decisions WHERE cycle = ?", (cycle,)).fetchone():
                raise ValueError("cycle has already been reviewed")
            active = RuntimeConfig.model_validate_json(
                db.execute("SELECT value FROM study_state WHERE key = 'active_config'").fetchone()[
                    0
                ]
            )
            if config_hash(active) != report["parent_hash"]:
                raise ValueError("active version changed since evaluation")
            db.execute(
                "INSERT INTO study_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(
                    decision[key]
                    for key in (
                        "decision_id",
                        "cycle",
                        "candidate_hash",
                        "report_hash",
                        "reviewer",
                        "accepted",
                        "reason",
                        "created_at",
                    )
                ),
            )
            if accepted:
                db.execute(
                    "UPDATE study_state SET value = ? WHERE key = 'active_config'",
                    (_json(report["candidate"]),),
                )
        return decision

    def reject_ineligible(self, cycle: int) -> dict[str, Any]:
        report = self.report(cycle)
        if report["promotion_eligible"]:
            raise ValueError("eligible proposals require an explicit human review")
        return self.review(
            cycle,
            report["report_hash"],
            report["candidate_hash"],
            "system:promotion-rule",
            False,
            "Automatic rejection under the fixed rule; this is not a human review.",
        )

    def rollback(self, version_hash: str, reviewer: str, reason: str) -> dict[str, str]:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        config: RuntimeConfig | None = INITIAL if config_hash(INITIAL) == version_hash else None
        with self.connect() as db:
            accepted_cycles = [
                row[0]
                for row in db.execute(
                    "SELECT cycle FROM study_decisions WHERE accepted = 1 AND candidate_hash = ?",
                    (version_hash,),
                )
            ]
        if config is None and accepted_cycles:
            report = self.report(accepted_cycles[-1])
            if report["candidate_hash"] == version_hash:
                config = RuntimeConfig.model_validate(report["candidate"])
        if config is None:
            raise ValueError("rollback target is not an accepted version")
        rollback_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE study_state SET value = ? WHERE key = 'active_config'",
                (_json(config.model_dump(mode="json")),),
            )
            db.execute(
                "INSERT INTO study_rollbacks VALUES (?, ?, ?, ?, ?)",
                (rollback_id, version_hash, reviewer, reason, datetime.now(UTC).isoformat()),
            )
        return {
            "rollback_id": rollback_id,
            "active_hash": version_hash,
            "effect": "configuration_pointer_only",
        }

    def history(self) -> dict[str, Any]:
        with self.connect() as db:
            cycles = [row[0] for row in db.execute("SELECT cycle FROM study_cycles ORDER BY cycle")]
            decisions = db.execute(
                "SELECT decision_id, cycle, candidate_hash, report_hash, reviewer, accepted, "
                "reason, created_at "
                "FROM study_decisions ORDER BY cycle"
            ).fetchall()
            attempts = db.execute(
                "SELECT attempt_id, cycle, provider, reserved_tool_calls, status "
                "FROM study_attempts ORDER BY rowid"
            ).fetchall()
            rollbacks = db.execute(
                "SELECT rollback_id, candidate_hash, reviewer, reason, created_at FROM study_rollbacks "
                "ORDER BY rowid"
            ).fetchall()
        keys = (
            "decision_id",
            "cycle",
            "candidate_hash",
            "report_hash",
            "reviewer",
            "accepted",
            "reason",
            "created_at",
        )
        reports = [self.report(cycle) for cycle in cycles]
        usage = [report["provider_usage"] for report in reports]
        unknown_cost = any(item.get("cost_usd") is None for item in usage) or any(
            row[4] != "completed" for row in attempts
        )
        return {
            "active_config": json.loads(self._state("active_config")),
            "tool_calls_reserved": int(self._state("tool_calls_reserved")),
            "proposal_calls": int(self._state("proposal_calls")),
            "final_opened": self._state("final_opened") == "1",
            "proposal_cost_usd": (
                None if unknown_cost else sum(item["cost_usd"] for item in usage)
            ),
            "cost_note": (
                "Unknown if a provider attempt was interrupted or pricing was not supplied."
            ),
            "cycles": reports,
            "decisions": [dict(zip(keys, row)) for row in decisions],
            "rollbacks": [
                dict(
                    zip(("rollback_id", "candidate_hash", "reviewer", "reason", "created_at"), row)
                )
                for row in rollbacks
            ],
            "attempts": [
                dict(zip(("attempt_id", "cycle", "provider", "reserved_tool_calls", "status"), row))
                for row in attempts
            ],
        }

    def final(self, cycle: int, report_hash: str, candidate_hash: str) -> dict[str, Any]:
        report = self.report(cycle)
        if report["report_hash"] != report_hash or report["candidate_hash"] != candidate_hash:
            raise ValueError("final selection does not match the reviewed proposal")
        if self._state("final_opened") == "1":
            raise ValueError("final test has already run or been opened")
        with self.connect() as db:
            accepted = db.execute(
                "SELECT 1 FROM study_decisions WHERE cycle = ? AND report_hash = ? "
                "AND candidate_hash = ? AND accepted = 1",
                (cycle, report_hash, candidate_hash),
            ).fetchone()
        if (
            not accepted
            or config_hash(RuntimeConfig.model_validate_json(self._state("active_config")))
            != candidate_hash
        ):
            raise ValueError("final test requires the currently active reviewed version")
        reserve = 27 * 8
        self._reserve(reserve)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT value FROM study_state WHERE key = 'final_opened'").fetchone()[0]
                == "1"
            ):
                raise ValueError("final test has already run or been opened")
            db.execute("UPDATE study_state SET value = '1' WHERE key = 'final_opened'")
        budget = Budget(reserve)
        cases = build_cases("final")
        proposed = _run(cases, RuntimeConfig.model_validate(report["candidate"]), budget)
        fixed = _run(cases, FIXED, budget)
        heuristic = _run(cases, HEURISTIC, budget)
        search_config = RuntimeConfig.model_validate(
            report["selection"]["methods"]["non_trace_search"]["config"]
        )
        search = _run(cases, search_config, budget)
        result = {
            "cycle": cycle,
            "candidate_hash": candidate_hash,
            "selection_report_hash": report_hash,
            "split": "final",
            "seed": SPLIT_SEEDS["final"],
            "proposed": _metrics(proposed),
            "strong_fixed": _metrics(fixed),
            "heuristic": _metrics(heuristic),
            "non_trace_search": _metrics(search),
            "paired": _paired(proposed, fixed),
            "paired_vs_heuristic": _paired(proposed, heuristic),
            "paired_vs_non_trace_search": _paired(proposed, search),
            "results": {
                "proposed": [item.model_dump(mode="json") for item in proposed],
                "strong_fixed": [item.model_dump(mode="json") for item in fixed],
                "heuristic": [item.model_dump(mode="json") for item in heuristic],
                "non_trace_search": [item.model_dump(mode="json") for item in search],
            },
            "tool_calls": budget.used,
        }
        with self.connect() as db:
            db.execute("INSERT INTO study_final VALUES (1, ?, ?)", (_json(result), _hash(result)))
        self._reconcile(reserve, budget.used)
        return result


def _run(
    cases: tuple[BenchmarkCase, ...], config: RuntimeConfig, budget: Budget
) -> list[BenchmarkResult]:
    return [run_case(case, config, budget) for case in cases]


def _diagnostics(
    cases: tuple[BenchmarkCase, ...], results: list[BenchmarkResult]
) -> list[dict[str, Any]]:
    diagnostics = []
    for case, result in zip(cases, results):
        if result.correct:
            continue
        cited = set(result.trace.references)
        stale = any(
            record.reference in cited
            and not record.valid_from <= case.task.as_of < record.valid_until
            for record in case.task.records
        )
        diagnostics.append(
            {
                "task_id": case.task.task_id,
                "family": case.task.family.value,
                "failure": "stale_reference" if stale else "missing_or_unsupported_evidence",
                "trace_references": list(result.trace.references),
                "tool_events": list(result.trace.events),
            }
        )
    return diagnostics


def _fake_proposal(
    diagnostics: list[dict[str, Any]], parent: RuntimeConfig, cycle: int
) -> tuple[CandidateProposal, dict[str, Any]]:
    has_stale = any(item["failure"] == "stale_reference" for item in diagnostics)
    strategy = "current_first" if has_stale else "cross_check"
    limit = min(3, parent.max_evidence_items + 1)
    proposal = CandidateProposal(
        retrieval_strategy=strategy,
        max_evidence_items=limit,
        verify_before_answer=cycle == 3,
        failure_references=[item["task_id"] for item in diagnostics[:8]],
        rationale="Use observed stale or incomplete evidence to select a bounded retrieval strategy.",
    )
    return proposal, {
        "provider": "deterministic_fake",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }


def _metrics(results: list[BenchmarkResult]) -> dict[str, Any]:
    count = len(results)
    return {
        "tasks": count,
        "correct": sum(result.correct for result in results),
        "accuracy": sum(result.correct for result in results) / count if count else 0.0,
        "abstentions": sum(result.trace.decision.value == "insufficient" for result in results),
        "failures": sum(result.trace.termination != "completed" for result in results),
        "unsupported_references": sum(not result.supported_references for result in results),
        "blocked_attempts": sum(result.trace.blocked_attempts for result in results),
        "executed_violations": sum(result.trace.executed_violations for result in results),
        "tool_calls": sum(result.trace.tool_calls for result in results),
        "synthetic_tool_cost_usd": 0.0,
        "synthetic_cost_per_verified_completion_usd": 0.0
        if any(item.correct for item in results)
        else None,
        "per_family": {
            family.value: {
                "tasks": len(items),
                "correct": sum(item.correct for item in items),
                "accuracy": sum(item.correct for item in items) / len(items) if items else 0.0,
                "tool_calls": sum(item.trace.tool_calls for item in items),
            }
            for family in TaskFamily
            if (items := [item for item in results if item.trace.family == family])
        },
    }


def _paired(left: list[BenchmarkResult], right: list[BenchmarkResult]) -> dict[str, Any]:
    differences = [int(a.correct) - int(b.correct) for a, b in zip(left, right)]
    mean = sum(differences) / len(differences)
    variance = sum((value - mean) ** 2 for value in differences) / (len(differences) - 1)
    return {
        "wins": sum(value == 1 for value in differences),
        "losses": sum(value == -1 for value in differences),
        "ties": sum(value == 0 for value in differences),
        "accuracy_delta": mean,
        "standard_error": (variance / len(differences)) ** 0.5,
    }


def _eligible(metrics: dict[str, dict[str, Any]], changed: bool) -> bool:
    candidate = metrics["proposed"]
    if not changed or candidate["executed_violations"]:
        return False
    for name in ("active", "strong_fixed", "heuristic", "non_trace_search"):
        other = metrics[name]
        if candidate["accuracy"] < other["accuracy"]:
            return False
        if any(
            candidate["per_family"][family]["accuracy"] < value["accuracy"]
            for family, value in other["per_family"].items()
        ):
            return False
    return all(
        candidate["accuracy"] > metrics[name]["accuracy"]
        or (
            candidate["accuracy"] == metrics[name]["accuracy"]
            and candidate["tool_calls"] < metrics[name]["tool_calls"]
        )
        for name in ("active", "strong_fixed")
    )
