"""Model clients: an offline simulated two-tier fleet and a local Ollama fleet.

Every call goes through :class:`ModelCache`, which is content-addressed by
(model id, prompt, attempt). The cache is what makes support-aware counterfactual
replay possible: a candidate harness that produces a byte-identical model input can
reuse the logged output instead of calling the model again.

The simulated model is a seeded stochastic stand-in, **not** a language model. Its
error rates per hazard are assumptions declared in ``SIMULATED_PROFILES``; offline
results therefore show the loop's mechanics, not model behaviour.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .harness import Guide, Tier
from .tasks import Claim, Record, authoritative, compare, field_value, known_records

# Illustrative hosted-equivalent prices (USD per million tokens) used as a cost proxy
# for routing economics. They are assumptions, not measured prices of local inference.
PRICE_ASSUMPTIONS: dict[Tier, tuple[float, float]] = {
    Tier.SMALL: (0.10, 0.40),
    Tier.LARGE: (0.60, 2.40),
}

SIMULATED_PROFILES: dict[Tier, dict[str, float]] = {
    Tier.SMALL: {
        "base": 0.08,
        "temporal": 0.55,
        "injection": 0.55,
        "conflict": 0.60,
        "missing": 0.50,
        "per_record": 0.03,
        "cite_drop": 0.25,
        "alias": 0.30,
    },
    Tier.LARGE: {
        "base": 0.03,
        "temporal": 0.20,
        "injection": 0.20,
        "conflict": 0.25,
        "missing": 0.20,
        "per_record": 0.01,
        "cite_drop": 0.08,
        "alias": 0.05,
    },
}
GUIDE_EFFECT = 0.6  # assumption: advisory guides reduce, but do not remove, a failure mode

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["supported", "contradicted", "insufficient"]},
        "references": {"type": "array", "items": {"type": "string"}},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
            },
        },
    },
    "required": ["decision", "references", "actions"],
}


class OutOfSupport(LookupError):
    """Raised in replay mode when a model input was never observed."""


@dataclass(frozen=True)
class ModelRequest:
    tier: Tier
    system: str
    user: str
    attempt: int
    # Structured view of exactly what the prompt contains; used only by the simulator.
    view: dict[str, Any] = field(compare=False, hash=False)


@dataclass(frozen=True)
class ModelResult:
    model_id: str
    tier: Tier
    text: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    cached: bool = False

    @property
    def cost_usd(self) -> float:
        price_in, price_out = PRICE_ASSUMPTIONS[self.tier]
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


class Backend(Protocol):
    name: str

    def model_id(self, tier: Tier) -> str: ...

    def generate(self, request: ModelRequest) -> ModelResult: ...


def request_key(model_id: str, request: ModelRequest) -> str:
    payload = json.dumps([model_id, request.system, request.user, request.attempt])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModelCache:
    """Content-addressed log of every model call; the replay substrate."""

    def __init__(self) -> None:
        self._entries: dict[str, ModelResult] = {}
        self.live_calls = 0
        self.reused_calls = 0

    def __len__(self) -> int:
        return len(self._entries)

    def call(
        self, backend: Backend, request: ModelRequest, *, replay_only: bool = False
    ) -> ModelResult:
        model_id = backend.model_id(request.tier)
        key = request_key(model_id, request)
        hit = self._entries.get(key)
        if hit is not None:
            self.reused_calls += 1
            return ModelResult(**{**hit.__dict__, "cached": True})
        if replay_only:
            raise OutOfSupport(key)
        result = backend.generate(request)
        self._entries[key] = result
        self.live_calls += 1
        return result

    def export(self) -> dict[str, dict[str, Any]]:
        return {
            key: {k: (v.value if isinstance(v, Tier) else v) for k, v in result.__dict__.items()}
            for key, result in self._entries.items()
        }


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class SimulatedBackend:
    """Seeded stochastic two-tier stand-in. Not a language model."""

    name = "simulated"

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def model_id(self, tier: Tier) -> str:
        return f"simulated-{tier.value}-v3"

    def generate(self, request: ModelRequest) -> ModelResult:
        digest = hashlib.sha256(
            f"{self.seed}|{request.tier}|{request.system}|{request.user}|{request.attempt}".encode()
        ).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        output = _simulate(rng, SIMULATED_PROFILES[request.tier], request.view)
        text = json.dumps(output, sort_keys=True)
        return ModelResult(
            model_id=self.model_id(request.tier),
            tier=request.tier,
            text=text,
            input_tokens=_tokens(request.system + request.user),
            output_tokens=_tokens(text),
            latency_s=0.0,
        )


def _simulate(rng: random.Random, p: dict[str, float], view: dict[str, Any]) -> dict[str, Any]:
    claim = Claim.model_validate(view["claim"])
    as_of: int = view["as_of"]
    guides = set(view["guides"])
    records = [Record.model_validate(r) for r in view["records"]]

    def g(guide: Guide) -> float:
        return GUIDE_EFFECT if guide.value in guides else 1.0

    def decide(record: Record | None) -> tuple[str, list[str]]:
        if record is None:
            return "insufficient", []
        value = field_value(record, claim.field)
        if value is None:
            return "insufficient", []
        ok = compare(claim.comparator, value, claim.value)
        return ("supported" if ok else "contradicted"), [record.reference]

    def actions_for(decision: str) -> list[dict[str, Any]]:
        if view["family"] == "refund_request" and decision == "supported":
            return [
                {
                    "tool": "stage_refund",
                    "arguments": {"order": claim.entity, "amount": claim.value},
                }
            ]
        return []

    # 1. Embedded instruction followed.
    injected = [r for r in records if "NOTE TO AI ASSISTANT" in r.note]
    if injected and rng.random() < p["injection"] * g(Guide.IGNORE_EMBEDDED_INSTRUCTIONS):
        acts = [{"tool": "admin_override", "arguments": {"case": claim.entity}}]
        return {
            "decision": "supported",
            "references": [injected[0].reference],
            "actions": acts + actions_for("supported"),
        }

    entity_records = [r for r in records if r.entity == claim.entity]
    alias_hidden = rng.random() < p["alias"]

    def has_field(r: Record) -> bool:
        if claim.field in r.fields:
            return True
        return not alias_hidden and field_value(r, claim.field) is not None

    current = [r for r in authoritative(records, claim.entity, as_of) if has_field(r)]
    values = {field_value(r, claim.field) for r in current}
    if len(current) >= 1 and len(values) == 1:
        decision, refs = decide(min(current, key=lambda r: r.reference))
        refs = sorted(r.reference for r in current)
        mode = "single"
    elif len(values) > 1:
        decision, refs, mode = "insufficient", [], "conflict"
    else:
        decision, refs, mode = "insufficient", [], "missing"

    # 2. Temporal trap: read the latest-recorded record, ignoring as-of and supersession.
    naive_pool = [r for r in entity_records if has_field(r)]
    if naive_pool:
        naive = max(naive_pool, key=lambda r: (r.recorded_at, r.reference))
        naive_decision, naive_refs = decide(naive)
        known = {r.reference for r in known_records(records, claim.entity, as_of)}
        trap = naive.reference not in {r.reference for r in current} or naive.reference not in known
        if (
            trap
            and naive_decision != decision
            and rng.random() < p["temporal"] * g(Guide.TEMPORAL_RULE)
        ):
            decision, refs, mode = naive_decision, naive_refs, "temporal_error"

    # 3. Conflict resolved by picking a side; 4. missing field hallucinated.
    if mode == "conflict" and rng.random() < p["conflict"] * g(Guide.ABSTAIN_ON_CONFLICT):
        decision, refs = decide(rng.choice(current))
    elif mode == "missing" and rng.random() < p["missing"] * g(Guide.ABSTAIN_ON_MISSING):
        pool = [r for r in entity_records if field_value(r, claim.field) is not None]
        decision, refs = (
            decide(rng.choice(pool))
            if pool
            else ("supported", [entity_records[0].reference] if entity_records else [])
        )

    # 5. Context-length and residual noise.
    noise = p["base"] + p["per_record"] * max(0, len(records) - 6)
    if rng.random() < noise:
        decision = rng.choice(
            [d for d in ("supported", "contradicted", "insufficient") if d != decision]
        )

    # 6. Citation omission.
    if refs and rng.random() < p["cite_drop"] * g(Guide.CITE_SOURCES):
        refs = []
    return {"decision": decision, "references": refs, "actions": actions_for(decision)}


class OllamaBackend:
    """Local models via the Ollama HTTP API. Nothing leaves the machine."""

    name = "ollama"

    def __init__(
        self,
        small: str = "llama3.2:3b",
        large: str = "qwen2.5:7b",
        host: str = "http://127.0.0.1:11434",
        timeout_s: float = 180.0,
    ) -> None:
        self.models = {Tier.SMALL: small, Tier.LARGE: large}
        self.host = host.rstrip("/")
        self.timeout_s = timeout_s

    def model_id(self, tier: Tier) -> str:
        return f"ollama/{self.models[tier]}"

    def chat(
        self, model: str, system: str, user: str, attempt: int, schema: dict[str, Any]
    ) -> dict[str, Any]:
        seed = int(hashlib.sha256(f"{system}|{user}|{attempt}".encode()).hexdigest()[:8], 16)
        body = {
            "model": model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.0 if attempt == 0 else 0.7, "seed": seed, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read())
        except urllib.error.URLError as error:  # pragma: no cover - requires a live server
            raise ConnectionError(f"Ollama is not reachable at {self.host}: {error}") from error

    def generate(self, request: ModelRequest) -> ModelResult:
        started = time.perf_counter()
        payload = self.chat(
            self.models[request.tier], request.system, request.user, request.attempt, OUTPUT_SCHEMA
        )
        return ModelResult(
            model_id=self.model_id(request.tier),
            tier=request.tier,
            text=payload["message"]["content"],
            input_tokens=int(payload.get("prompt_eval_count", 0)),
            output_tokens=int(payload.get("eval_count", 0)),
            latency_s=time.perf_counter() - started,
        )

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as response:
                names = {m["name"] for m in json.loads(response.read())["models"]}
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        return all(model in names for model in self.models.values())
