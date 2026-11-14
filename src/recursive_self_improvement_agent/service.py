from __future__ import annotations

from typing import Any

from .models import RuntimeConfig
from .store import Registry


def make_candidate(
    registry: Registry,
    parent_version_id: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"retrieval_strategy", "max_evidence_items", "verify_before_answer"}
    if not changes or set(changes) - allowed:
        raise ValueError("candidate changes may contain only bounded runtime strategy fields")
    parent, _ = registry.get_version(parent_version_id)
    config = RuntimeConfig.model_validate({**parent.model_dump(), **changes})
    if config == parent:
        raise ValueError("version changes must alter the runtime configuration")
    candidate_id = registry.add_version(config, parent_version_id)
    return {
        "version_id": candidate_id,
        "parent_version_id": parent_version_id,
        "diff": changes,
        "config_hash": registry.get_version(candidate_id)[1],
    }
