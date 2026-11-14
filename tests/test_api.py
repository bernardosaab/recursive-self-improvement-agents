from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from recursive_self_improvement_agent import api
from recursive_self_improvement_agent.models import RuntimeConfig
from recursive_self_improvement_agent.store import Registry


def test_local_api_exposes_bounded_version_changes(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "api.sqlite3")
    root = RuntimeConfig(
        retrieval_strategy="single_pass",
        max_evidence_items=1,
        verify_before_answer=False,
    )
    root_id = registry.add_version(root)
    registry.register_initial(root_id)
    monkeypatch.setattr(api, "registry", registry)
    client = TestClient(api.app)

    created = client.post(
        "/versions",
        json={
            "parent_version_id": root_id,
            "changes": {"retrieval_strategy": "current_first"},
        },
    )
    prohibited = client.post(
        "/versions",
        json={
            "parent_version_id": root_id,
            "changes": {"tool_permissions": ["read_evaluator_labels"]},
        },
    )

    assert created.status_code == 200
    assert created.json()["diff"] == {"retrieval_strategy": "current_first"}
    assert prohibited.status_code == 422
    assert client.get("/versions").json()["active_version_id"] == root_id
