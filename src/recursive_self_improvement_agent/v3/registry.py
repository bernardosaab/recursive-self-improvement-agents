"""Bi-temporal harness registry with hash-chained lineage and PROV-style export.

* ``versions``: immutable harness configurations, content-addressed.
* ``activations``: which harness an arm runs, as validity intervals. Activating or
  rolling back closes the open interval (sets ``valid_to``) and opens a new one;
  rows are never deleted or reopened ("invalidate, do not delete").
* ``events``: append-only, hash-chained log of diagnose/propose/evaluate/review/
  activate activities, from which reconstruction packs and PROV JSON are built.

Times are a logical clock supplied by the caller so evidence runs are reproducible.
This is a local SQLite record, not a tamper-proof audit store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .harness import HarnessConfig, harness_hash

GENESIS = "0" * 64


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Registry:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS versions (
                version_hash TEXT PRIMARY KEY, config_json TEXT NOT NULL,
                parent_hash TEXT, created_tx INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, arm TEXT NOT NULL,
                version_hash TEXT NOT NULL REFERENCES versions(version_hash),
                valid_from INTEGER NOT NULL, valid_to INTEGER, tx_time INTEGER NOT NULL,
                reason TEXT NOT NULL, decision_event INTEGER);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, tx_time INTEGER NOT NULL, arm TEXT NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS no_event_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS no_event_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS no_activation_delete BEFORE DELETE ON activations
                BEGIN SELECT RAISE(ABORT, 'activations are invalidated, never deleted'); END;
            CREATE TRIGGER IF NOT EXISTS close_once BEFORE UPDATE ON activations
                WHEN OLD.valid_to IS NOT NULL OR NEW.version_hash != OLD.version_hash
                  OR NEW.valid_from != OLD.valid_from
                BEGIN SELECT RAISE(ABORT, 'an activation interval can only be closed once'); END;
            """
        )
        self.clock = 0

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    # -- versions ------------------------------------------------------------
    def add_version(self, config: HarnessConfig, parent: str | None = None) -> str:
        digest = harness_hash(config)
        self.db.execute(
            "INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?)",
            (digest, config.model_dump_json(), parent, self.clock),
        )
        self.db.commit()
        return digest

    def get_version(self, digest: str) -> HarnessConfig:
        row = self.db.execute(
            "SELECT config_json FROM versions WHERE version_hash = ?", (digest,)
        ).fetchone()
        if row is None:
            raise KeyError(digest)
        config = HarnessConfig.model_validate_json(row[0])
        if harness_hash(config) != digest:
            raise ValueError("stored harness does not match its hash")
        return config

    # -- events ----------------------------------------------------------------
    def log(self, arm: str, kind: str, payload: dict[str, Any]) -> int:
        row = self.db.execute(
            "SELECT seq, entry_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
        tx = self.tick()
        body = _canonical(payload)
        entry = hashlib.sha256(_canonical([seq, tx, arm, kind, body, prev]).encode()).hexdigest()
        self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, tx, arm, kind, body, prev, entry),
        )
        self.db.commit()
        return seq

    def verify_chain(self) -> bool:
        prev = GENESIS
        for seq, tx, arm, kind, body, prev_hash, entry in self.db.execute(
            "SELECT * FROM events ORDER BY seq"
        ):
            if prev_hash != prev:
                return False
            if (
                hashlib.sha256(_canonical([seq, tx, arm, kind, body, prev]).encode()).hexdigest()
                != entry
            ):
                return False
            prev = entry
        return True

    def events(self, arm: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT seq, tx_time, arm, kind, payload_json, entry_hash FROM events"
        rows = self.db.execute(
            query + (" WHERE arm = ? ORDER BY seq" if arm else " ORDER BY seq"),
            (arm,) if arm else (),
        )
        return [
            {"seq": s, "tx_time": t, "arm": a, "kind": k, "payload": json.loads(p), "entry_hash": h}
            for s, t, a, k, p, h in rows
        ]

    # -- activations (bi-temporal) -------------------------------------------------
    def activate(
        self, arm: str, digest: str, reason: str, decision_event: int | None = None
    ) -> None:
        self.get_version(digest)
        now = self.tick()
        self.db.execute(
            "UPDATE activations SET valid_to = ? WHERE arm = ? AND valid_to IS NULL", (now, arm)
        )
        self.db.execute(
            "INSERT INTO activations (arm, version_hash, valid_from, valid_to, tx_time, reason, decision_event) VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (arm, digest, now, now, reason, decision_event),
        )
        self.db.commit()
        self.log(
            arm,
            "activate",
            {"version_hash": digest, "reason": reason, "decision_event": decision_event},
        )

    def active(self, arm: str) -> str | None:
        row = self.db.execute(
            "SELECT version_hash FROM activations WHERE arm = ? AND valid_to IS NULL", (arm,)
        ).fetchone()
        return row[0] if row else None

    def as_of(self, arm: str, t: int) -> str | None:
        row = self.db.execute(
            "SELECT version_hash FROM activations WHERE arm = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ORDER BY valid_from DESC LIMIT 1",
            (arm, t, t),
        ).fetchone()
        return row[0] if row else None

    def history(self, arm: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT version_hash, valid_from, valid_to, reason FROM activations WHERE arm = ? ORDER BY id",
            (arm,),
        )
        return [
            {"version_hash": v, "valid_from": f, "valid_to": t, "reason": r} for v, f, t, r in rows
        ]

    def accepted_versions(self, arm: str) -> set[str]:
        return {h["version_hash"] for h in self.history(arm)}

    def rollback(self, arm: str, digest: str, reviewer: str, reason: str) -> None:
        if digest not in self.accepted_versions(arm):
            raise ValueError("rollback target was never an accepted, active version of this arm")
        seq = self.log(
            arm, "rollback_review", {"to": digest, "reviewer": reviewer, "reason": reason}
        )
        self.activate(arm, digest, f"rollback: {reason}", seq)

    # -- review ------------------------------------------------------------------
    def review(
        self,
        arm: str,
        candidate: str,
        report_hash: str,
        reviewer: str,
        accepted: bool,
        reason: str,
        mode: str,
    ) -> int:
        """Bind a decision to the exact candidate and report hashes."""
        return self.log(
            arm,
            "review",
            {
                "candidate_hash": candidate,
                "report_hash": report_hash,
                "reviewer": reviewer,
                "accepted": accepted,
                "reason": reason,
                "mode": mode,
            },
        )

    # -- exports -------------------------------------------------------------------
    def reconstruction_pack(self, arm: str, review_seq: int) -> dict[str, Any]:
        events = self.events(arm)
        review = next(e for e in events if e["seq"] == review_seq)
        candidate = review["payload"]["candidate_hash"]
        related = [
            e
            for e in events
            if e["payload"].get("candidate_hash") == candidate or e["seq"] == review_seq
        ]
        evaluation = next((e for e in related if e["kind"] == "evaluate"), None)
        proposal = next((e for e in related if e["kind"] == "propose"), None)
        diagnosis = None
        if proposal is not None:
            diagnosis = next(
                (
                    e
                    for e in reversed(events)
                    if e["kind"] == "diagnose" and e["seq"] < proposal["seq"]
                ),
                None,
            )
        return {
            "candidate_hash": candidate,
            "candidate_config": self.get_version(candidate).model_dump(mode="json"),
            "diagnosis": diagnosis,
            "proposal": proposal,
            "evaluation": evaluation,
            "review": review,
            "activation": next(
                (
                    e
                    for e in events
                    if e["kind"] == "activate" and e["payload"].get("decision_event") == review_seq
                ),
                None,
            ),
            "chain_valid": self.verify_chain(),
        }

    def prov(self) -> dict[str, Any]:
        """W3C PROV-JSON-style lineage of versions, reports and decisions."""
        entity: dict[str, Any] = {}
        activity: dict[str, Any] = {}
        agent: dict[str, Any] = {"agent:trusted_system": {"prov:type": "prov:SoftwareAgent"}}
        generated: dict[str, Any] = {}
        derived: dict[str, Any] = {}
        associated: dict[str, Any] = {}
        for v, cfg, parent, created in self.db.execute("SELECT * FROM versions"):
            entity[f"harness:{v[:16]}"] = {"prov:type": "harness_version", "created_tx": created}
            if parent:
                derived[f"_:d{v[:8]}"] = {
                    "prov:generatedEntity": f"harness:{v[:16]}",
                    "prov:usedEntity": f"harness:{parent[:16]}",
                }
        for e in self.events():
            act = f"activity:{e['seq']}"
            activity[act] = {
                "prov:type": e["kind"],
                "arm": e["arm"],
                "tx_time": e["tx_time"],
                "entry_hash": e["entry_hash"],
            }
            who = e["payload"].get("reviewer") or e["payload"].get("source")
            if who:
                agent.setdefault(
                    f"agent:{who}",
                    {
                        "prov:type": "prov:Person"
                        if e["kind"] == "review" and e["payload"].get("mode") != "system"
                        else "prov:SoftwareAgent"
                    },
                )
                associated[f"_:a{e['seq']}"] = {"prov:activity": act, "prov:agent": f"agent:{who}"}
            else:
                associated[f"_:a{e['seq']}"] = {
                    "prov:activity": act,
                    "prov:agent": "agent:trusted_system",
                }
            cand = e["payload"].get("candidate_hash") or e["payload"].get("version_hash")
            if cand:
                generated[f"_:g{e['seq']}"] = {
                    "prov:entity": f"harness:{cand[:16]}",
                    "prov:activity": act,
                }
        return {
            "prefix": {
                "harness": "urn:harness:",
                "activity": "urn:activity:",
                "agent": "urn:agent:",
            },
            "entity": entity,
            "activity": activity,
            "agent": agent,
            "wasGeneratedBy": generated,
            "wasDerivedFrom": derived,
            "wasAssociatedWith": associated,
        }
