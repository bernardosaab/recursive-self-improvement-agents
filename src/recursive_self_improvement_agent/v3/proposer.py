"""Candidate generators and experiential proposer memory.

All proposers emit untrusted :class:`HarnessProposal` objects that trusted code
validates and applies. Offline proposers are deterministic rules (stand-ins, not
reasoning); the Ollama proposer is a real local model.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .diagnostics import FailureCluster
from .harness import (
    ALL_OPS,
    EDIT_OP_ADAPTER,
    PROMPT_ONLY_OPS,
    FailAction,
    Family,
    Guide,
    HarnessConfig,
    HarnessProposal,
    Hazard,
    ProposalRejected,
    SensorKind,
    Tier,
    apply_proposal,
    harness_hash,
    parse_op,
)
from .models import CHECKPOINT, PRICE_ASSUMPTIONS, OllamaBackend, memo_call


@dataclass
class MemoryEntry:
    signature: str
    source: str
    signals: tuple[str, ...]
    ops: list[dict[str, Any]]
    stage: str  # invalid | screened_out | rejected | accepted | pending
    utility: float
    reason: str


@dataclass
class ProposalMemory:
    """Experiential memory: what was tried, its measured utility and why it failed."""

    enabled: bool = True
    entries: list[MemoryEntry] = field(default_factory=list)

    def add(self, entry: MemoryEntry) -> None:
        self.entries.append(entry)

    def rejected_signatures(self) -> set[str]:
        if not self.enabled:
            return set()
        return {
            e.signature for e in self.entries if e.stage in {"invalid", "screened_out", "rejected"}
        }

    def exemplars(self, k: int = 3) -> dict[str, list[dict[str, Any]]]:
        if not self.enabled or not self.entries:
            return {"helpful": [], "unhelpful": []}
        ranked = sorted(self.entries, key=lambda e: e.utility, reverse=True)

        def show(e: MemoryEntry) -> dict[str, Any]:
            return {
                "ops": e.ops,
                "signals": list(e.signals),
                "stage": e.stage,
                "utility": round(e.utility, 4),
                "reason": e.reason,
            }

        return {
            "helpful": [show(e) for e in ranked[:k] if e.utility > 0],
            "unhelpful": [show(e) for e in ranked[::-1][:k] if e.utility <= 0],
        }


@dataclass
class ProposerUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    invalid_outputs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Proposer(Protocol):
    name: str
    allowed_ops: frozenset[str]
    usage: ProposerUsage

    def propose(
        self,
        parent: HarnessConfig,
        clusters: list[FailureCluster],
        trace_summaries: list[dict[str, Any]],
        memory: ProposalMemory,
        k: int,
        cycle: int,
    ) -> list[HarnessProposal | dict[str, Any]]: ...


def _op(**kwargs: Any) -> dict[str, Any]:
    return kwargs


# Structural repair table: failure signal -> alternative edits, most targeted first.
STRUCTURAL_REPAIRS: dict[str, list[list[dict[str, Any]]]] = {
    "tool_fault_unrecovered": [[_op(op="set_tool_retries", retries=1)]],
    "invalid_output": [
        [_op(op="add_sensor", kind=SensorKind.SCHEMA_VALID, on_fail=FailAction.RETRY)]
    ],
    "followed_embedded_instruction": [
        [
            _op(op="set_quarantine", enabled=True),
            _op(op="add_sensor", kind=SensorKind.ACTIONS_PERMITTED, on_fail=FailAction.RETRY),
        ],
        [_op(op="add_escalation", hazard=Hazard.INJECTION)],
    ],
    "late_record_used": [
        [_op(op="set_context", field="as_of_filter", value=True)],
        [_op(op="add_sensor", kind=SensorKind.REFS_TEMPORALLY_VALID, on_fail=FailAction.ESCALATE)],
    ],
    "superseded_record_used": [
        [
            _op(op="set_context", field="as_of_filter", value=True),
            _op(op="set_context", field="drop_superseded", value=True),
        ],
        [_op(op="add_sensor", kind=SensorKind.REFS_NOT_SUPERSEDED, on_fail=FailAction.RETRY)],
    ],
    "conflict_not_abstained": [
        [
            _op(
                op="add_sensor",
                kind=SensorKind.CONFLICT_REQUIRES_ABSTAIN,
                on_fail=FailAction.ABSTAIN,
            )
        ],
        [_op(op="add_escalation", hazard=Hazard.CONFLICT)],
    ],
    "missing_field_asserted": [
        [_op(op="add_sensor", kind=SensorKind.REFS_CONTAIN_FIELD, on_fail=FailAction.ABSTAIN)]
    ],
    "missed_alias_field": [[_op(op="set_default_tier", tier=Tier.LARGE)]],
    "uncited_or_wrong_citation": [
        [_op(op="add_sensor", kind=SensorKind.REFS_RETRIEVED, on_fail=FailAction.RETRY)]
    ],
    "wrong_write": [
        [_op(op="add_sensor", kind=SensorKind.ACTIONS_PERMITTED, on_fail=FailAction.RETRY)]
    ],
    "residual_decision_error": [
        [
            _op(op="set_context", field="entity_filter", value=True),
            _op(op="set_context", field="max_records", value=6),
        ],
        [_op(op="set_checker", checker="large"), _op(op="set_check_when", check_when="on_hazard")],
        [_op(op="add_escalation", hazard=Hazard.LONG_CONTEXT)],
        [_op(op="set_default_tier", tier=Tier.LARGE)],
    ],
}

PROMPT_REPAIRS: dict[str, list[list[dict[str, Any]]]] = {
    "late_record_used": [[_op(op="add_guide", guide=Guide.TEMPORAL_RULE)]],
    "superseded_record_used": [[_op(op="add_guide", guide=Guide.TEMPORAL_RULE)]],
    "followed_embedded_instruction": [
        [_op(op="add_guide", guide=Guide.IGNORE_EMBEDDED_INSTRUCTIONS)]
    ],
    "conflict_not_abstained": [[_op(op="add_guide", guide=Guide.ABSTAIN_ON_CONFLICT)]],
    "missing_field_asserted": [[_op(op="add_guide", guide=Guide.ABSTAIN_ON_MISSING)]],
    "uncited_or_wrong_citation": [[_op(op="add_guide", guide=Guide.CITE_SOURCES)]],
    "_fallback": [
        [
            _op(
                op="set_extra_guidance",
                text="Read every record carefully, check the comparator twice and answer insufficient when unsure.",
            )
        ]
    ],
}


def _try_build(
    parent: HarnessConfig,
    ops: list[dict[str, Any]],
    signals: list[str],
    source: str,
    allowed: frozenset[str],
) -> HarnessProposal | None:
    try:
        proposal = HarnessProposal(
            parent_hash=harness_hash(parent),
            ops=tuple(EDIT_OP_ADAPTER.validate_python(o) for o in ops),
            cited_failures=tuple(signals),
            rationale=f"Targets development failure clusters: {', '.join(signals)}.",
            source=source,
        )
        apply_proposal(parent, proposal, allowed)
    except (ValidationError, ProposalRejected):
        return None
    return proposal


class TableProposer:
    """Deterministic, trace-informed rule proposer (structural or prompt-only)."""

    def __init__(
        self, name: str, table: dict[str, list[list[dict[str, Any]]]], allowed_ops: frozenset[str]
    ) -> None:
        self.name = name
        self.table = table
        self.allowed_ops = allowed_ops
        self.usage = ProposerUsage()

    def _options(
        self, parent: HarnessConfig, signal: str, memory: ProposalMemory
    ) -> list[HarnessProposal]:
        rejected = memory.rejected_signatures()
        options = []
        for ops in self.table.get(signal, []) + (
            self.table.get("_fallback", []) if signal not in self.table else []
        ):
            proposal = _try_build(parent, ops, [signal], self.name, self.allowed_ops)
            if proposal is not None and proposal.signature() not in rejected:
                options.append(proposal)
        return options

    def propose(self, parent, clusters, trace_summaries, memory, k, cycle):
        self.usage.calls += 1
        proposals: list[HarnessProposal] = []
        firsts: list[HarnessProposal] = []
        for cluster in clusters:
            options = self._options(parent, cluster.signal, memory)
            if options:
                firsts.append(options[0])
        if firsts:
            proposals.append(firsts[0])
        if len(firsts) >= 2 and len(proposals) < k:
            merged_ops = [o.model_dump(mode="python") for p in firsts[:3] for o in p.ops]
            unique = list(
                {json.dumps(o, sort_keys=True, default=str): o for o in merged_ops}.values()
            )[:6]
            signals = [s for p in firsts[:3] for s in p.cited_failures]
            merged = _try_build(parent, unique, signals, self.name, self.allowed_ops)
            if merged is not None and merged.signature() not in memory.rejected_signatures():
                proposals.append(merged)
        for extra in firsts[1:]:
            if len(proposals) >= k:
                break
            if extra.signature() not in {p.signature() for p in proposals}:
                proposals.append(extra)
        return proposals[:k]


def structural_proposer() -> TableProposer:
    return TableProposer("harness_ratchet_rules", STRUCTURAL_REPAIRS, ALL_OPS)


def prompt_only_proposer() -> TableProposer:
    return TableProposer("prompt_reflection_rules", PROMPT_REPAIRS, PROMPT_ONLY_OPS)


class RandomProposer:
    """Non-trace baseline: uniformly random valid edits from the full language."""

    name = "random_dsl_search"
    allowed_ops = ALL_OPS

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self.usage = ProposerUsage()

    def _random_op(self, rng: random.Random) -> dict[str, Any]:
        choice = rng.choice(
            sorted(
                ALL_OPS
                - {"set_extra_guidance", "remove_guide", "remove_sensor", "remove_escalation"}
            )
        )
        match choice:
            case "add_guide":
                return _op(op=choice, guide=rng.choice(list(Guide)))
            case "set_context":
                fld = rng.choice(
                    ["as_of_filter", "drop_superseded", "entity_filter", "max_records", "order"]
                )
                value: Any = {
                    "max_records": rng.randint(2, 12),
                    "order": rng.choice(["relevance", "recency"]),
                }.get(fld, rng.random() < 0.5)
                return _op(op=choice, field=fld, value=value)
            case "set_quarantine":
                return _op(op=choice, enabled=rng.random() < 0.5)
            case "add_sensor":
                return _op(
                    op=choice,
                    kind=rng.choice(list(SensorKind)),
                    on_fail=rng.choice(list(FailAction)),
                )
            case "set_default_tier":
                return _op(op=choice, tier=rng.choice(list(Tier)))
            case "add_escalation":
                return _op(op=choice, hazard=rng.choice(list(Hazard)))
            case "set_tool_retries":
                return _op(op=choice, retries=rng.randint(0, 2))
            case "set_checker":
                return _op(op=choice, checker=rng.choice(["none", "small", "large"]))
            case "set_check_when":
                return _op(op=choice, check_when=rng.choice(["always", "on_hazard", "on_write"]))
            case _:
                return _op(
                    op="add_compiled",
                    family=rng.choice([Family.INVOICE, Family.REFUND, Family.TRIAGE]),
                )

    def propose(self, parent, clusters, trace_summaries, memory, k, cycle):
        self.usage.calls += 1
        rng = random.Random(self.seed * 1000 + cycle)
        proposals: list[HarnessProposal] = []
        attempts = 0
        while len(proposals) < k and attempts < 200:
            attempts += 1
            ops = [self._random_op(rng) for _ in range(rng.randint(1, 3))]
            proposal = _try_build(parent, ops, ["random"], self.name, self.allowed_ops)
            if proposal is not None and proposal.signature() not in {
                p.signature() for p in proposals
            }:
                proposals.append(proposal)
        return proposals


FLAT_OP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"type": "string"},
        "guide": {"type": "string", "enum": [g.value for g in Guide]},
        "text": {"type": "string"},
        "field": {
            "type": "string",
            "enum": ["as_of_filter", "drop_superseded", "entity_filter", "max_records", "order"],
        },
        "value": {},
        "enabled": {"type": "boolean"},
        "kind": {"type": "string", "enum": [k.value for k in SensorKind]},
        "on_fail": {"type": "string", "enum": [a.value for a in FailAction]},
        "tier": {"type": "string", "enum": [t.value for t in Tier]},
        "hazard": {"type": "string", "enum": [h.value for h in Hazard]},
        "retries": {"type": "integer"},
        "checker": {"type": "string", "enum": ["none", "small", "large"]},
        "check_when": {"type": "string", "enum": ["always", "on_hazard", "on_write"]},
        "family": {
            "type": "string",
            "enum": [Family.INVOICE.value, Family.REFUND.value, Family.TRIAGE.value],
        },
    },
    "required": ["op"],
}
OP_FIELDS = {
    "add_guide": ("guide",),
    "remove_guide": ("guide",),
    "set_extra_guidance": ("text",),
    "set_context": ("field", "value"),
    "set_quarantine": ("enabled",),
    "add_sensor": ("kind", "on_fail"),
    "remove_sensor": ("kind",),
    "set_default_tier": ("tier",),
    "add_escalation": ("hazard",),
    "remove_escalation": ("hazard",),
    "set_tool_retries": ("retries",),
    "set_checker": ("checker",),
    "set_check_when": ("check_when",),
    "add_compiled": ("family",),
}

OLLAMA_SYSTEM = (
    "You improve an agent HARNESS (not the model). You may only emit typed edit operations. "
    "Harness parts: guides (advisory instructions), context policy (as_of_filter drops records "
    "not yet known/valid on the as-of day; drop_superseded drops records replaced by a visible "
    "newer record; entity_filter keeps only the claimed entity; max_records 2-12; order), "
    "quarantine (removes instruction-like text from records), sensors (deterministic checks on "
    "the model answer with on_fail retry|escalate|abstain), routing (default tier small|large and "
    "hazards that escalate to large; large costs about 6x), tool_retries (0-2), compiled recipes "
    "(deterministic known path per family; skips to the model when preconditions fail), team "
    "(an independent checker role, none|small|large, reviewing the answer always, on_hazard "
    "or on_write; an objection escalates the maker once or abstains). Prefer "
    "structural fixes that make a failure impossible over advice. Avoid edits listed as "
    'unhelpful. Reply with JSON: {"ops": [...1-4 ops...], "cited_failures": [cluster ids], '
    '"rationale": "..."}.'
)


class OllamaProposer:
    """A real local model proposes edits; output is validated like any untrusted input."""

    def __init__(
        self,
        backend: OllamaBackend,
        name: str = "harness_ratchet_ollama",
        allowed_ops: frozenset[str] = ALL_OPS,
    ) -> None:
        self.backend = backend
        self.name = name
        self.allowed_ops = allowed_ops
        self.usage = ProposerUsage()
        self.namespace = CHECKPOINT.namespace("proposer")

    def propose(self, parent, clusters, trace_summaries, memory, k, cycle):
        schema = {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        **FLAT_OP_SCHEMA,
                        "properties": {
                            **FLAT_OP_SCHEMA["properties"],
                            "op": {"type": "string", "enum": sorted(self.allowed_ops)},
                        },
                    },
                },
                "cited_failures": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
            },
            "required": ["ops", "cited_failures", "rationale"],
        }
        user = json.dumps(
            {
                "allowed_ops": {op: OP_FIELDS[op] for op in sorted(self.allowed_ops)},
                "current_harness": parent.model_dump(mode="json"),
                "failure_clusters": [c.model_dump(mode="json") for c in clusters[:6]],
                "failed_trace_samples": trace_summaries[:8],
                "memory": memory.exemplars(),
            },
            sort_keys=True,
        )
        results: list[HarnessProposal | dict[str, Any]] = []
        for attempt in range(k):
            started = time.perf_counter()
            seed = attempt + 100 * cycle
            model = self.backend.models[Tier.LARGE]
            payload = memo_call(
                self.namespace,
                [model, OLLAMA_SYSTEM, user, seed, schema],
                lambda model=model, seed=seed: self.backend.chat(
                    model, OLLAMA_SYSTEM, user, seed, schema, max_tokens=1024
                ),
            )
            self.usage.calls += 1
            self.usage.input_tokens += int(payload.get("prompt_eval_count", 0))
            self.usage.output_tokens += int(payload.get("eval_count", 0))
            self.usage.latency_s += time.perf_counter() - started
            price_in, price_out = PRICE_ASSUMPTIONS[Tier.LARGE]
            self.usage.cost_usd = (
                self.usage.input_tokens * price_in + self.usage.output_tokens * price_out
            ) / 1e6
            text = payload["message"]["content"]
            try:
                raw = json.loads(text)
                ops = [
                    {key: o[key] for key in ("op", *OP_FIELDS.get(o.get("op"), ())) if key in o}
                    for o in raw.get("ops", [])
                ]
                proposal = HarnessProposal(
                    parent_hash=harness_hash(parent),
                    ops=tuple(parse_op(o) for o in ops),
                    cited_failures=tuple(str(c) for c in raw.get("cited_failures", []))[:12]
                    or ("unspecified",),
                    rationale=str(raw.get("rationale", ""))[:600] or "no rationale",
                    source=self.name,
                )
                results.append(proposal)
            except (ValueError, ValidationError, AttributeError, TypeError) as error:
                self.usage.invalid_outputs += 1
                results.append(
                    {
                        "invalid": True,
                        "source": self.name,
                        "error": str(error)[:300],
                        "raw": text[:600],
                    }
                )
        return results
