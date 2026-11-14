from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evaluation import MAX_EXPERIMENT_TOOL_CALLS, report_hash
from .models import EvaluationReport, PromotionDecision, RuntimeConfig
from .runtime import config_hash

DEFAULT_DB = Path(os.environ.get("RASIS_DB_PATH", "data/runtime.sqlite3"))


class Registry:
    """Append-only local records; the active version pointer is the sole mutable registry state."""

    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT PRIMARY KEY,
                    parent_version_id TEXT,
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    candidate_version_id TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS version_diffs (
                    version_id TEXT PRIMARY KEY,
                    diff_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    candidate_version_id TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registry_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def add_version(self, config: RuntimeConfig, parent_version_id: str | None = None) -> str:
        digest = config_hash(config)
        version_id = f"v-{digest[:12]}"
        with self._connect() as connection:
            diff: dict[str, Any] = {}
            if parent_version_id is not None:
                parent_row = connection.execute(
                    "SELECT config_json FROM versions WHERE version_id = ?", (parent_version_id,)
                ).fetchone()
                if parent_row is None:
                    raise ValueError("parent version does not exist")
                parent = json.loads(parent_row[0])
                diff = {
                    key: value
                    for key, value in config.model_dump(mode="json").items()
                    if value != parent[key]
                }
            connection.execute(
                "INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?, ?)",
                (
                    version_id,
                    parent_version_id,
                    _canonical(config.model_dump(mode="json")),
                    digest,
                    _now(),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO version_diffs VALUES (?, ?)",
                (version_id, _canonical(diff)),
            )
        return version_id

    def register_initial(self, version_id: str) -> None:
        if self.active_version() is not None:
            return
        _, digest = self.get_version(version_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    str(uuid.uuid4()),
                    version_id,
                    digest,
                    "initialization",
                    hashlib.sha256(b"initialization").hexdigest(),
                    "local-demo-bootstrap",
                    "Initial fixed reference configuration.",
                    _now(),
                ),
            )
            connection.execute(
                "INSERT INTO registry_state VALUES ('active_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (version_id,),
            )

    def get_version(self, version_id: str) -> tuple[RuntimeConfig, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT config_json, config_hash, parent_version_id FROM versions "
                "WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            diff_row = connection.execute(
                "SELECT diff_json FROM version_diffs WHERE version_id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        config = RuntimeConfig.model_validate_json(row[0])
        if config_hash(config) != row[1] or version_id != f"v-{row[1][:12]}":
            raise ValueError("stored version hash mismatch")
        expected_diff: dict[str, Any] = {}
        if row[2] is not None:
            with self._connect() as connection:
                parent = connection.execute(
                    "SELECT config_json, config_hash FROM versions WHERE version_id = ?",
                    (row[2],),
                ).fetchone()
            if parent is None:
                raise ValueError("stored version parent is missing")
            parent_config = RuntimeConfig.model_validate_json(parent[0])
            if config_hash(parent_config) != parent[1] or row[2] != f"v-{parent[1][:12]}":
                raise ValueError("stored parent version hash mismatch")
            parent_values = parent_config.model_dump(mode="json")
            expected_diff = {
                key: value
                for key, value in config.model_dump(mode="json").items()
                if value != parent_values[key]
            }
        if diff_row is None or json.loads(diff_row[0]) != expected_diff:
            raise ValueError("stored version diff mismatch")
        return config, row[1]

    def list_versions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT v.version_id, v.parent_version_id, v.config_json, v.config_hash, "
                "v.created_at, d.diff_json FROM versions v "
                "JOIN version_diffs d ON d.version_id = v.version_id "
                "ORDER BY v.created_at, v.version_id"
            ).fetchall()
        for row in rows:
            self.get_version(row[0])
        return [
            {
                "version_id": row[0],
                "parent_version_id": row[1],
                "config": json.loads(row[2]),
                "config_hash": row[3],
                "created_at": row[4],
                "diff": json.loads(row[5]),
            }
            for row in rows
        ]

    def list_decisions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT decision_id, candidate_version_id, candidate_hash, report_id, "
                "report_hash, reviewer, accepted, reason, created_at "
                "FROM decisions ORDER BY created_at, decision_id"
            ).fetchall()
        return [
            {
                "decision_id": row[0],
                "candidate_version_id": row[1],
                "candidate_hash": row[2],
                "report_id": row[3],
                "report_hash": row[4],
                "reviewer": row[5],
                "accepted": bool(row[6]),
                "reason": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    def add_report(self, report: EvaluationReport) -> str:
        _, candidate_digest = self.get_version(report.candidate_version_id)
        _, baseline_digest = self.get_version(report.baseline_version_id)
        if candidate_digest != report.candidate_hash or baseline_digest != report.baseline_hash:
            raise ValueError("report configuration hashes do not match immutable versions")
        self._validate_heuristic_comparison(report)
        if self.total_tool_calls() + _report_tool_calls(report) > MAX_EXPERIMENT_TOOL_CALLS:
            raise ValueError("experiment-wide tool-call budget exhausted")
        serialized = _canonical(report.model_dump(mode="json"))
        digest = report_hash(report)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?)",
                (report.report_id, report.candidate_version_id, serialized, digest, _now()),
            )
        return digest

    def total_tool_calls(self) -> int:
        with self._connect() as connection:
            rows = connection.execute("SELECT report_json FROM reports").fetchall()
        return sum(_report_tool_calls(EvaluationReport.model_validate_json(row[0])) for row in rows)

    def require_tool_budget(self, additional_calls: int) -> None:
        if (
            additional_calls < 0
            or self.total_tool_calls() + additional_calls > MAX_EXPERIMENT_TOOL_CALLS
        ):
            raise ValueError("experiment-wide tool-call budget would be exceeded")

    def get_report(self, report_id: str) -> tuple[EvaluationReport, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json, report_hash FROM reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            raise KeyError(report_id)
        report = EvaluationReport.model_validate_json(row[0])
        if report_hash(report) != row[1]:
            raise ValueError("stored report hash mismatch")
        self._validate_heuristic_comparison(report)
        return report, row[1]

    def decide(
        self,
        candidate_version_id: str,
        report_id: str,
        reviewer: str,
        accepted: bool,
        reason: str,
    ) -> PromotionDecision:
        config, candidate_hash = self.get_version(candidate_version_id)
        del config
        report, digest = self.get_report(report_id)
        if report.candidate_version_id != candidate_version_id:
            raise ValueError("report does not evaluate this exact candidate")
        if report.candidate_hash != candidate_hash:
            raise ValueError("report configuration hash does not match the reviewed version")
        if accepted and not report.promotion_eligible:
            raise ValueError("candidate does not satisfy the predeclared promotion rule")
        decision = PromotionDecision(
            decision_id=str(uuid.uuid4()),
            candidate_version_id=candidate_version_id,
            candidate_hash=candidate_hash,
            report_id=report_id,
            report_hash=digest,
            reviewer=reviewer,
            accepted=accepted,
            reason=reason,
            created_at=_now(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    decision.candidate_version_id,
                    decision.candidate_hash,
                    decision.report_id,
                    decision.report_hash,
                    decision.reviewer,
                    int(decision.accepted),
                    decision.reason,
                    decision.created_at,
                ),
            )
            if accepted:
                connection.execute(
                    "INSERT INTO registry_state VALUES ('active_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (candidate_version_id,),
                )
        return decision

    def has_accepted_review(self, version_id: str, report_id: str) -> bool:
        _, version_digest = self.get_version(version_id)
        report, digest = self.get_report(report_id)
        if (
            report.split != "selection"
            or report.candidate_version_id != version_id
            or report.candidate_hash != version_digest
        ):
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM decisions WHERE candidate_version_id = ? "
                "AND candidate_hash = ? AND report_id = ? AND report_hash = ? AND accepted = 1",
                (version_id, version_digest, report_id, digest),
            ).fetchone()
        return row is not None

    def has_finalized_selection(self, report_id: str) -> bool:
        with self._connect() as connection:
            rows = connection.execute("SELECT report_json FROM reports").fetchall()
        return any(
            EvaluationReport.model_validate_json(row[0]).selection_report_id == report_id
            for row in rows
        )

    def rollback(self, version_id: str, reviewer: str, reason: str) -> dict[str, str]:
        self.get_version(version_id)
        with self._connect() as connection:
            accepted = connection.execute(
                "SELECT 1 FROM decisions WHERE candidate_version_id = ? AND accepted = 1",
                (version_id,),
            ).fetchone()
            if accepted is None:
                raise ValueError("rollback target is not a previously accepted version")
            connection.execute(
                "INSERT INTO registry_state VALUES ('active_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (version_id,),
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    str(uuid.uuid4()),
                    version_id,
                    self.get_version(version_id)[1],
                    "rollback",
                    hashlib.sha256(b"rollback").hexdigest(),
                    reviewer,
                    f"Rollback pointer change only: {reason}",
                    _now(),
                ),
            )
        return {"active_version_id": version_id, "effect": "configuration_pointer_only"}

    def active_version(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM registry_state WHERE key = 'active_version'"
            ).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _validate_heuristic_comparison(self, report: EvaluationReport) -> None:
        comparison = report.heuristic_comparison
        if comparison is None:
            return
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json, report_hash FROM reports WHERE report_id = ?",
                (comparison.report_id,),
            ).fetchone()
        if row is None:
            raise ValueError("heuristic comparison report is missing")
        heuristic = EvaluationReport.model_validate_json(row[0])
        if (
            report_hash(heuristic) != row[1]
            or row[1] != comparison.report_hash
            or heuristic.candidate_version_id != comparison.version_id
            or heuristic.candidate_hash != comparison.version_hash
        ):
            raise ValueError("heuristic comparison report hash mismatch")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _report_tool_calls(report: EvaluationReport) -> int:
    return sum(
        pair.proposed_result.trace.tool_calls + pair.baseline_result.trace.tool_calls
        for pair in report.task_results
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
