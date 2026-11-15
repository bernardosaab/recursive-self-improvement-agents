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
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .harness import Guide, Tier
from .tasks import Claim, Record, authoritative, compare, field_value, known_records, resolve

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
        "check_detect": 0.55,
        "check_false_alarm": 0.06,
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
        "check_detect": 0.85,
        "check_false_alarm": 0.03,
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


class ChunkDeadline(RuntimeError):
    """Raised before a live call once the invocation's time box is spent."""


class CallStore:
    """Durable, content-addressed store of every live model call (the checkpoint).

    Because tools are deterministic and every model input is content-addressed, a
    re-run that restores calls from this store follows exactly the same path as an
    uninterrupted run, so long studies can be split into short resumable chunks.
    """

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls (namespace TEXT, key TEXT, payload TEXT, "
            "PRIMARY KEY (namespace, key))"
        )
        self.db.commit()

    def load(self, namespace: str) -> dict[str, str]:
        rows = self.db.execute("SELECT key, payload FROM calls WHERE namespace = ?", (namespace,))
        return dict(rows.fetchall())

    def put(self, namespace: str, key: str, payload: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO calls VALUES (?, ?, ?)", (namespace, key, payload))
        self.db.commit()

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0])


@dataclass
class Checkpoint:
    store: CallStore | None = None
    deadline: float | None = None
    counter: int = 0
    new_calls: int = 0
    max_new_calls: int | None = None

    def namespace(self, prefix: str = "c") -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def check(self) -> None:
        """Called before every new live call; each chunk always makes progress."""
        spent = (self.deadline is not None and time.monotonic() >= self.deadline) or (
            self.max_new_calls is not None and self.new_calls >= self.max_new_calls
        )
        if spent and self.new_calls > 0:
            raise ChunkDeadline("time box spent; re-run the same command to resume")
        self.new_calls += 1

    def reset(
        self,
        store: CallStore | None = None,
        max_seconds: float | None = None,
        max_new_calls: int | None = None,
    ) -> None:
        self.store = store
        self.max_new_calls = max_new_calls
        self.deadline = time.monotonic() + max_seconds if max_seconds else None
        self.counter = 0
        self.new_calls = 0


CHECKPOINT = Checkpoint()


def _dump_result(result: ModelResult) -> str:
    return json.dumps({**result.__dict__, "tier": result.tier.value, "cached": False})


def _load_result(payload: str) -> ModelResult:
    data = json.loads(payload)
    data["tier"] = Tier(data["tier"])
    return ModelResult(**data)


class ModelCache:
    """Content-addressed log of every model call; the replay substrate and checkpoint."""

    def __init__(self) -> None:
        self._entries: dict[str, ModelResult] = {}
        self.live_calls = 0
        self.reused_calls = 0
        self.namespace = CHECKPOINT.namespace()
        self._restorable = CHECKPOINT.store.load(self.namespace) if CHECKPOINT.store else {}

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
        restored = self._restorable.pop(key, None)
        if restored is not None:
            result = _load_result(restored)  # counted as live: it was live when first made
        else:
            CHECKPOINT.check()
            result = backend.generate(request)
            if CHECKPOINT.store is not None:
                CHECKPOINT.store.put(self.namespace, key, _dump_result(result))
        self._entries[key] = result
        self.live_calls += 1
        return result

    def export(self) -> dict[str, dict[str, Any]]:
        return {
            key: {k: (v.value if isinstance(v, Tier) else v) for k, v in result.__dict__.items()}
            for key, result in self._entries.items()
        }


def memo_call(namespace: str, key_parts: list[Any], fn: Any) -> dict[str, Any]:
    """Checkpointed wrapper for non-runtime model calls (e.g. the proposer)."""
    key = hashlib.sha256(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()
    if CHECKPOINT.store is not None:
        stored = CHECKPOINT.store.load(namespace).get(key)
        if stored is not None:
            return json.loads(stored)
    CHECKPOINT.check()
    started = time.perf_counter()
    payload = fn()
    payload["_wall_s"] = time.perf_counter() - started  # preserved across resumes
    if CHECKPOINT.store is not None:
        CHECKPOINT.store.put(namespace, key, json.dumps(payload))
    return payload


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class SimulatedBackend:
    """Seeded stochastic two-tier stand-in. Not a language model."""

    name = "simulated"

    def __init__(self, seed: int = 0, guide_effect: float = GUIDE_EFFECT) -> None:
        self.seed = seed
        self.guide_effect = guide_effect

    def model_id(self, tier: Tier) -> str:
        suffix = "" if self.guide_effect == GUIDE_EFFECT else f"-guide{self.guide_effect:g}"
        return f"simulated-{tier.value}-v3{suffix}"

    def generate(self, request: ModelRequest) -> ModelResult:
        digest = hashlib.sha256(
            f"{self.seed}|{request.tier}|{request.system}|{request.user}|{request.attempt}".encode()
        ).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        output = _simulate(rng, SIMULATED_PROFILES[request.tier], request.view, self.guide_effect)
        text = json.dumps(output, sort_keys=True)
        return ModelResult(
            model_id=self.model_id(request.tier),
            tier=request.tier,
            text=text,
            input_tokens=_tokens(request.system + request.user),
            output_tokens=_tokens(text),
            latency_s=0.0,
        )


def _simulate_checker(
    rng: random.Random, p: dict[str, float], view: dict[str, Any]
) -> dict[str, Any]:
    claim = Claim.model_validate(view["claim"])
    records = [Record.model_validate(r) for r in view["records"]]
    careful, refs = resolve(claim, view["as_of"], records)
    proposed = view["proposed"]
    wrong = proposed["decision"] != careful or (
        careful != "insufficient" and set(proposed["references"]) != set(refs)
    )
    flagged = rng.random() < (p["check_detect"] if wrong else p["check_false_alarm"])
    return {
        "agree": not flagged,
        "decision": careful if flagged else proposed["decision"],
        "reason": "simulated review",
    }


def _simulate(
    rng: random.Random,
    p: dict[str, float],
    view: dict[str, Any],
    guide_effect: float = GUIDE_EFFECT,
) -> dict[str, Any]:
    if view.get("role") == "checker":
        return _simulate_checker(rng, p, view)
    claim = Claim.model_validate(view["claim"])
    as_of: int = view["as_of"]
    guides = set(view["guides"])
    records = [Record.model_validate(r) for r in view["records"]]

    def g(guide: Guide) -> float:
        return guide_effect if guide.value in guides else 1.0

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
        num_ctx: int = 4096,
    ) -> None:
        self.models = {Tier.SMALL: small, Tier.LARGE: large}
        self.host = host.rstrip("/")
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx  # both tiers must fit in 8 GB VRAM together

    def model_id(self, tier: Tier) -> str:
        return f"ollama/{self.models[tier]}"

    def chat(
        self,
        model: str,
        system: str,
        user: str,
        attempt: int,
        schema: dict[str, Any],
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        seed = int(hashlib.sha256(f"{system}|{user}|{attempt}".encode()).hexdigest()[:8], 16)
        body = {
            "model": model,
            "stream": False,
            "format": schema,
            "options": {
                "temperature": 0.0 if attempt == 0 else 0.7,
                "seed": seed,
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,  # caps runaway generation; truncation = invalid
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = json.dumps(body).encode("utf-8")
        last: Exception | None = None
        for wait in (0, 5, 15, 30):  # Ollama recovers from GPU OOM by unloading models
            if wait:
                time.sleep(wait)
            request = urllib.request.Request(
                f"{self.host}/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:  # pragma: no cover - live server only
                last = error
                if error.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as error:  # pragma: no cover
                last = error
        raise ConnectionError(f"Ollama request failed at {self.host}: {last}")  # pragma: no cover

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
