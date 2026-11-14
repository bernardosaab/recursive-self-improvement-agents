from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .models import CandidateProposal, RuntimeConfig


class ProposalProvider(Protocol):
    def propose(
        self, diagnostics: list[dict[str, Any]]
    ) -> tuple[CandidateProposal, dict[str, Any]]: ...


class DeterministicFakeProvider:
    def propose(
        self, diagnostics: list[dict[str, Any]]
    ) -> tuple[CandidateProposal, dict[str, Any]]:
        references = [
            item["task_id"]
            for item in diagnostics
            if item["failure"] == "missed_or_ambiguous_evidence"
        ][:8]
        if not references:
            if not diagnostics:
                raise ValueError("no development failures are available to diagnose")
            references = [item["task_id"] for item in diagnostics[:1]]
        stale_leading_record = any(
            reference.endswith("-decoy")
            for item in diagnostics
            for reference in item["trace_references"]
        )
        strategy = "current_first" if stale_leading_record else "cross_check"
        rationale = (
            "Development traces cite a stale leading record; retrieve current evidence first."
            if stale_leading_record
            else "Development traces show ambiguous evidence; cross-check retrieved sources."
        )
        return (
            CandidateProposal(
                retrieval_strategy=strategy,
                max_evidence_items=2,
                verify_before_answer=not stale_leading_record,
                failure_references=references,
                rationale=rationale,
            ),
            {
                "provider": "deterministic_fake",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        )


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """Explicit opt-in; the only submitted content is synthetic development diagnostics."""

    api_key: str
    model: str
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @classmethod
    def from_environment(cls) -> OpenAICompatibleProvider:
        api_key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL")
        if not api_key or not model:
            raise ValueError("set OPENAI_API_KEY and OPENAI_MODEL to enable the real provider")
        return cls(
            api_key=api_key,
            model=model,
            endpoint=os.environ.get(
                "OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions"
            ),
            input_price_per_million=_optional_float("MODEL_INPUT_USD_PER_MILLION"),
            output_price_per_million=_optional_float("MODEL_OUTPUT_USD_PER_MILLION"),
        )

    def propose(
        self, diagnostics: list[dict[str, Any]]
    ) -> tuple[CandidateProposal, dict[str, Any]]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "retrieval_strategy": {
                    "type": ["string", "null"],
                    "enum": ["single_pass", "cross_check", "current_first", None],
                },
                "max_evidence_items": {"type": ["integer", "null"], "minimum": 1, "maximum": 3},
                "verify_before_answer": {"type": ["boolean", "null"]},
                "failure_references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": [
                "retrieval_strategy",
                "max_evidence_items",
                "verify_before_answer",
                "failure_references",
                "rationale",
            ],
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": 500,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "bounded_runtime_proposal",
                    "strict": True,
                    "schema": schema,
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": "Propose only a typed runtime strategy change. Never request new tools, labels, scope, budgets, or evaluator changes.",
                },
                {"role": "user", "content": json.dumps(diagnostics, separators=(",", ":"))},
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        content = payload["choices"][0]["message"]["content"]
        proposal = CandidateProposal.model_validate_json(content)
        usage = payload["usage"]
        input_tokens = int(usage["prompt_tokens"])
        output_tokens = int(usage["completion_tokens"])
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("provider returned invalid token usage")
        known_pricing = (
            self.input_price_per_million is not None and self.output_price_per_million is not None
        )
        cost = (
            input_tokens * self.input_price_per_million / 1_000_000
            + output_tokens * self.output_price_per_million / 1_000_000
            if known_pricing
            else None
        )
        return proposal, {
            "provider": "openai_compatible",
            "model": self.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "pricing_assumption": {
                "input_usd_per_million": self.input_price_per_million,
                "output_usd_per_million": self.output_price_per_million,
            },
            "cost_usd": cost,
        }


def build_diagnostics(results: tuple[Any, ...]) -> list[dict[str, Any]]:
    diagnostics = []
    for result in results:
        if not result.correct:
            diagnostics.append(
                {
                    "task_id": result.task_id,
                    "family": result.family.value,
                    "failure": "missed_or_ambiguous_evidence",
                    "expected_available_to_agent": False,
                    "trace_references": [
                        reference
                        for event in result.trace.tool_events
                        for reference in event.result_references
                    ],
                    "termination": result.trace.termination,
                }
            )
    return diagnostics


def apply_proposal(parent: RuntimeConfig, proposal: CandidateProposal) -> RuntimeConfig:
    values = parent.model_dump()
    values.update(proposal.changes())
    proposed = RuntimeConfig.model_validate(values)
    if proposed == parent:
        raise ValueError("proposal does not change the runtime configuration")
    return proposed


def validate_proposal(proposal: CandidateProposal, diagnostics: list[dict[str, Any]]) -> None:
    allowed_references = {item["task_id"] for item in diagnostics}
    if not set(proposal.failure_references).issubset(allowed_references):
        raise ValueError("proposal cites failures absent from development diagnostics")


def _optional_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed
