from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient

from recursive_agent_self_improvement import api
from recursive_agent_self_improvement.benchmark import (
    TOOLS,
    BenchmarkTrace,
    Budget,
    ScopedExecutor,
    build_cases,
    run_case,
    verify,
)
from recursive_agent_self_improvement.improvement import OpenAICompatibleProvider
from recursive_agent_self_improvement.models import Decision, RuntimeConfig, TaskFamily
from recursive_agent_self_improvement.runtime import config_hash
from recursive_agent_self_improvement.store import Registry
from recursive_agent_self_improvement.study import FIXED, INITIAL, Study


def test_families_use_distinct_scoped_tools() -> None:
    cases = build_cases("development")
    assert len(TOOLS) == 3
    assert len(set(TOOLS.values())) == 3
    for family in TaskFamily:
        case = next(case for case in cases if case.task.family == family)
        executor = ScopedExecutor(case.task, Budget(5))
        other_tool = next(tool for other, tool in TOOLS.items() if other != family)
        with pytest.raises(PermissionError, match="outside scope"):
            executor.call(case.task.task_id, other_tool, "all", 2)
        assert executor.events[-1]["outcome"] == "blocked"


def test_verifier_rejects_stale_and_unretrieved_citations() -> None:
    case = next(
        case
        for case in build_cases("development")
        if case.task.family == TaskFamily.EVIDENCE_RECONCILIATION
        and case.expected == Decision.SUPPORTED
    )
    stale = case.task.records[0].reference
    trace = BenchmarkTrace(
        task_id=case.task.task_id,
        family=case.task.family,
        events=({"tool": "search_documents", "outcome": "ok", "references": [stale]},),
        decision=case.expected,
        references=(stale,),
        termination="completed",
        tool_calls=1,
        blocked_attempts=0,
        executed_violations=0,
    )
    result = verify(case, trace)
    assert result.valid_references
    assert not result.supported_references
    assert not result.correct
    unsupported = trace.model_copy(update={"references": tuple(case.supporting_references)})
    assert not verify(case, unsupported).valid_references
    assert not verify(case, trace.model_copy(update={"references": ()})).correct


def test_missing_information_and_exhaustion_abstain() -> None:
    case = next(
        case for case in build_cases("development") if case.expected == Decision.INSUFFICIENT
    )
    result = run_case(case, FIXED, Budget(5), prohibited_attempt=True)
    assert result.correct
    assert result.trace.decision == Decision.INSUFFICIENT
    assert result.trace.blocked_attempts == 1
    assert result.trace.executed_violations == 0
    exhausted = run_case(case, FIXED, Budget(0))
    assert not exhausted.correct
    assert "budget exhausted" in exhausted.trace.termination
    assert exhausted.trace.tool_calls == 0


def test_final_contains_an_unseen_source_overload_variant() -> None:
    final = build_cases("final")
    development = build_cases("development")
    for family in TaskFamily:
        final_max = max(
            sum(record.fields.get("source_status") == "unverified" for record in case.task.records)
            for case in final
            if case.task.family == family
        )
        development_max = max(
            sum(record.fields.get("source_status") == "unverified" for record in case.task.records)
            for case in development
            if case.task.family == family
        )
        assert final_max > development_max


def test_three_bounded_cycles_preserve_negative_results_and_review_binding(tmp_path: Path) -> None:
    study = Study(tmp_path / "study.sqlite3")
    for cycle in range(1, 4):
        report = study.cycle()
        assert report["cycle"] == cycle
        assert report["adversarial"]["metrics"]["blocked_attempts"] == 3
        assert report["adversarial"]["metrics"]["executed_violations"] == 0
        assert report["selection"]["methods"]["strong_fixed"]["config"] == FIXED.model_dump()
        assert not report["promotion_eligible"]
        with pytest.raises(ValueError, match="exact report"):
            study.review(cycle, "wrong", report["candidate_hash"], "reviewer", False, "reason")
        with pytest.raises(ValueError, match="fixed promotion rule"):
            study.review(
                cycle, report["report_hash"], report["candidate_hash"], "reviewer", True, "reason"
            )
        study.review(
            cycle, report["report_hash"], report["candidate_hash"], "reviewer", False, "negative"
        )
    history = study.history()
    assert len(history["cycles"]) == len(history["decisions"]) == 3
    assert history["proposal_calls"] == 3
    assert history["proposal_cost_usd"] == 0.0
    assert history["tool_calls_reserved"] == sum(
        cycle["budget"]["tool_calls_this_cycle"] for cycle in history["cycles"]
    )
    with pytest.raises(ValueError, match="maximum selection cycles"):
        study.cycle()
    last = history["cycles"][-1]
    with pytest.raises(ValueError, match="currently active reviewed"):
        study.final(3, last["report_hash"], last["candidate_hash"])


def test_cycle_requires_prior_review_and_review_view_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "study.sqlite3"
    study = Study(path)
    report = study.cycle()
    with pytest.raises(ValueError, match="review the previous cycle"):
        study.cycle()
    monkeypatch.setattr(api, "registry", Registry(path))
    client = TestClient(api.app)
    response = client.get("/study/review/1")
    assert response.status_code == 200
    assert "Evidence-linked development diagnostics" in response.text
    assert "Blocked requests" in response.text
    assert report["report_hash"] in response.text
    response = client.post(
        "/study/reviews",
        json={
            "cycle": 1,
            "report_hash": report["report_hash"],
            "candidate_hash": report["candidate_hash"],
            "reviewer": "local",
            "accepted": False,
            "reason": "No improvement over the strong fixed strategy.",
        },
    )
    assert response.status_code == 200
    assert client.get("/study").json()["decisions"][0]["accepted"] == 0


def test_final_requires_exact_accepted_review_and_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the final-test gate independently of this benchmark's negative promotion result.
    monkeypatch.setattr("recursive_agent_self_improvement.study._eligible", lambda *_: True)
    study = Study(tmp_path / "study.sqlite3")
    report = study.cycle()
    with pytest.raises(ValueError, match="currently active reviewed"):
        study.final(1, report["report_hash"], report["candidate_hash"])
    study.review(1, report["report_hash"], report["candidate_hash"], "test-reviewer", True, "test")
    final = study.final(1, report["report_hash"], report["candidate_hash"])
    assert final["split"] == "final"
    assert final["proposed"]["tasks"] == 27
    with pytest.raises(ValueError, match="already run"):
        study.final(1, report["report_hash"], report["candidate_hash"])
    rollback = study.rollback(config_hash(INITIAL), "test-reviewer", "restore")
    assert rollback["effect"] == "configuration_pointer_only"
    assert study.history()["active_config"] == INITIAL.model_dump(mode="json")


def test_interrupted_final_stays_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("recursive_agent_self_improvement.study._eligible", lambda *_: True)
    study = Study(tmp_path / "study.sqlite3")
    report = study.cycle()
    study.review(1, report["report_hash"], report["candidate_hash"], "test-reviewer", True, "test")
    monkeypatch.setattr(
        "recursive_agent_self_improvement.study.build_cases",
        lambda *_: (_ for _ in ()).throw(RuntimeError("simulated interruption")),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        study.final(1, report["report_hash"], report["candidate_hash"])
    assert study.history()["final_opened"]
    with pytest.raises(ValueError, match="already run or been opened"):
        study.final(1, report["report_hash"], report["candidate_hash"])
    with pytest.raises(ValueError, match="already been opened"):
        study.cycle()


def test_opt_in_provider_parses_actual_usage_without_sending_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "retrieval_strategy": "cross_check",
                                        "max_evidence_items": 3,
                                        "verify_before_answer": False,
                                        "failure_references": ["synthetic-task"],
                                        "rationale": "Collect a missing source.",
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
            ).encode()

    def open_mock(request: Any, timeout: int) -> Response:
        sent.update(json.loads(request.data))
        assert timeout == 30
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_mock)
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        input_price_per_million=2.0,
        output_price_per_million=4.0,
    )
    proposal, usage = provider.propose(
        [
            {
                "task_id": "synthetic-task",
                "failure": "missing_or_unsupported_evidence",
                "trace_references": [],
            }
        ]
    )
    assert proposal.retrieval_strategy == "cross_check"
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["cost_usd"] == pytest.approx(0.0004)
    assert "test-key" not in json.dumps(sent)
    assert "expected" not in sent["messages"][1]["content"]


def test_published_selection_evidence_reproduces() -> None:
    path = Path(__file__).resolve().parents[1] / "evidence" / "study-v2-offline.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    assert evidence["proposal_calls"] == 3
    assert len(evidence["cycles"]) == 3
    for report in evidence["cycles"]:
        stored_hash = report.pop("report_hash")
        serialized = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert hashlib.sha256(serialized.encode()).hexdigest() == stored_hash
        assert not report["promotion_eligible"]
        cases = build_cases("selection", report["selection_seed"])
        for name, method in report["selection"]["methods"].items():
            config = RuntimeConfig.model_validate(method["config"])
            actual = [run_case(case, config, Budget(3)).model_dump(mode="json") for case in cases]
            assert actual == report["selection"]["task_results"][name]
