"""Robustness checks that address the study's two main threats to validity.

1. ``sweep``: the offline simulator *assumes* advisory guides only partly reduce a
   failure mode. This re-runs the whole offline study across task seeds and across
   guide-strength assumptions, from "guides do nothing" to "guides are almost as
   strong as structural fixes", to show which conclusions depend on that assumption.
2. ``confirm``: the real-model final split is small. This freezes the harnesses
   published in a report (their hashes are verified) and evaluates them once on
   fresh final-split task instances never used anywhere before.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Self

from .harness import HarnessConfig, RoutingPolicy, TeamPolicy, Tier, harness_hash
from .models import ModelCache
from .registry import Registry
from .stats import wilson
from .study import ArmLedger, Evaluator, Study, StudyConfig, _paired, make_backend
from .tasks import HELD_OUT_FAMILY, SPLIT_SEEDS, build_split
from .verifier import summarize


def _sweep_key(effect: float, scale: float, scales: list[float]) -> str:
    return str(effect) if scales == [1.0] else f"{effect}|{scale}"


def sweep(
    seeds: list[int],
    guide_effects: list[float],
    arms: tuple[str, ...],
    base: StudyConfig | None = None,
    error_scales: list[float] | None = None,
) -> dict[str, Any]:
    base = base or StudyConfig()
    scales = error_scales or [1.0]
    runs = []
    for scale in scales:
        for effect in guide_effects:
            for seed in seeds:
                config = dataclasses.replace(
                    base,
                    provider="simulated",
                    seed=seed,
                    task_seed_offset=1000 * seed,
                    guide_effect=effect,
                    error_scale=scale,
                    arms=arms,
                    approval="standing",
                    audits=False,
                )
                final = Study(config, Registry()).run()["final"]["arms"]
                row: dict[str, Any] = {"guide_effect": effect, "error_scale": scale, "seed": seed}
                for arm in arms:
                    row[arm] = final[arm]["overall"]["accuracy"]
                    row[f"{arm}_held_out"] = final[arm]["held_out_family"]["accuracy"]
                if "ratchet_vs_prompt_only" in final:
                    p = final["ratchet_vs_prompt_only"]
                    row["ratchet_minus_prompt_only"] = p["accuracy_delta"]
                    row["ratchet_vs_prompt_only_p"] = p["mcnemar_exact_p"]
                runs.append(row)
    summary = {}
    for scale in scales:
        for effect in guide_effects:
            rows = [r for r in runs if r["guide_effect"] == effect and r["error_scale"] == scale]
            entry: dict[str, Any] = {"guide_effect": effect, "error_scale": scale}
            for arm in arms:
                values = [r[arm] for r in rows]
                entry[arm] = {
                    "mean": statistics.fmean(values),
                    "min": min(values),
                    "max": max(values),
                    "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
                }
            if rows and "ratchet_minus_prompt_only" in rows[0]:
                entry["ratchet_beats_prompt_only_p_lt_0.05"] = sum(
                    r["ratchet_minus_prompt_only"] > 0 and r["ratchet_vs_prompt_only_p"] < 0.05
                    for r in rows
                )
                entry["seeds"] = len(rows)
            summary[_sweep_key(effect, scale, scales)] = entry
    return {
        "seeds": seeds,
        "guide_effects": guide_effects,
        "error_scales": scales,
        "arms": list(arms),
        "runs": runs,
        "summary": summary,
    }


def render_sweep(result: dict[str, Any]) -> str:
    arms = result["arms"]
    lines = [
        "# Offline sensitivity sweep",
        "",
        (
            f"Seeds {result['seeds']} (each seed shifts every split seed by 1000×seed and reseeds "
            "the simulator). Guide effect = multiplier on a failure mode's probability when the "
            "matching advisory guide is present (1.0 = guides do nothing, 0.6 = the published "
            "default, 0.0 = a guide fully removes its failure mode). Error scale = multiplier on "
            "every simulated hazard error rate of both tiers (1.0 = the published profile). "
            "Sealed final accuracy across seeds: mean [min–max]."
        ),
        "",
        "| Error scale | Guide effect | "
        + " | ".join(arms)
        + " | Ratchet > prompt-only (p<0.05) |",
        "|---:|---:|" + "---:|" * len(arms) + "---:|",
    ]
    for key, entry in result["summary"].items():
        effect = entry.get("guide_effect", key)
        scale = entry.get("error_scale", 1.0)
        cells = [
            f"{100 * entry[a]['mean']:.1f}% [{100 * entry[a]['min']:.0f}–{100 * entry[a]['max']:.0f}]"
            for a in arms
        ]
        wins = f"{entry.get('ratchet_beats_prompt_only_p_lt_0.05', 'n/a')}/{entry.get('seeds', '')}"
        lines.append(f"| {scale} | {effect} | " + " | ".join(cells) + f" | {wins} |")
    return "\n".join(lines) + "\n"


class PowerSampler:
    """Samples GPU board power with nvidia-smi once per second (best effort)."""

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _read(self) -> float | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            value = float(out.strip().splitlines()[0])
            # Laptop drivers occasionally report a spurious first reading (hundreds of watts
            # at idle); discard values outside a plausible board-power range.
            return value if 0.0 < value < 400.0 else None
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            value = self._read()
            if value is not None:
                self.samples.append(value)
            self._stop.wait(self.interval_s)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


VARIANTS = ("small_only", "checker_on_hazard", "checker_on_write")


def _variant(harness: HarnessConfig, name: str) -> HarnessConfig:
    if name == "small_only":
        return harness.model_copy(update={"routing": RoutingPolicy(default_tier=Tier.SMALL)})
    if name == "checker_on_hazard":
        return harness.model_copy(
            update={"team": TeamPolicy(checker="large", check_when="on_hazard")}
        )
    if name == "checker_on_write":
        return harness.model_copy(
            update={"team": TeamPolicy(checker="large", check_when="on_write")}
        )
    raise ValueError(f"unknown variant {name}")


def confirm(
    report_path: Path,
    arms: list[str],
    n_final: int,
    task_seed_offset: int,
    *,
    small_model: str | None = None,
    large_model: str | None = None,
    variants: tuple[str, ...] = ("small_only",),
    measure_power: bool = False,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fields = {f.name for f in dataclasses.fields(StudyConfig)}
    stored = {k: v for k, v in report["config"].items() if k in fields}
    stored["arms"] = tuple(stored.get("arms", ()))
    overrides: dict[str, Any] = {"n_final": n_final, "task_seed_offset": task_seed_offset}
    if small_model:
        overrides["small_model"] = small_model
    if large_model:
        overrides["large_model"] = large_model
    config = StudyConfig(**{**stored, **overrides})
    backend = make_backend(config)
    cases = list(build_split("final", n_final, seed=SPLIT_SEEDS["final"] + task_seed_offset))
    harnesses: dict[str, HarnessConfig] = {}
    for arm in arms:
        harness = HarnessConfig.model_validate_json(
            json.dumps(report["arms"][arm]["final_harness"])
        )
        if harness_hash(harness) != report["arms"][arm]["final_hash"]:
            raise ValueError(f"frozen harness for {arm} does not match its published hash")
        harnesses[arm] = harness
    if "ratchet" in harnesses:
        for name in variants:
            harnesses[f"ratchet@{name}"] = _variant(harnesses["ratchet"], name)
    outcomes = {}
    results: dict[str, Any] = {}
    for name, harness in harnesses.items():
        sampler = PowerSampler() if measure_power else None
        start = time.perf_counter()
        if sampler:
            with sampler:
                outs = Evaluator(backend, ModelCache(), ArmLedger()).run(cases, harness)[1]
        else:
            outs = Evaluator(backend, ModelCache(), ArmLedger()).run(cases, harness)[1]
        wall = time.perf_counter() - start
        outcomes[name] = outs
        held = [o for o in outs if o.family == HELD_OUT_FAMILY.value]
        completions = sum(o.verified_completion for o in outs)
        entry: dict[str, Any] = {
            "harness_hash": harness_hash(harness),
            "overall": summarize(outs),
            "accuracy_ci95": wilson(sum(o.correct for o in outs), len(outs)),
            "held_out_family": summarize(held),
            "compiled_tasks": summarize([o for o in outs if o.compiled]),
            "model_answered_tasks": summarize([o for o in outs if not o.compiled]),
            "wall_seconds": wall,
            "seconds_per_verified_completion": wall / completions if completions else None,
        }
        if sampler and sampler.samples:
            mean_w = statistics.fmean(sampler.samples)
            entry["gpu_power"] = {
                "samples": len(sampler.samples),
                "mean_watts": mean_w,
                "energy_wh": mean_w * wall / 3600,
                "wh_per_verified_completion": (mean_w * wall / 3600) / completions
                if completions
                else None,
            }
        results[name] = entry
    for ref in ("fixed", "ratchet"):
        if ref not in outcomes:
            continue
        base = outcomes[ref]
        base_held = [o for o in base if o.family == HELD_OUT_FAMILY.value]
        for name, outs in outcomes.items():
            if name == ref or (ref == "ratchet" and not name.startswith("ratchet@")):
                continue
            results[name][f"vs_{ref}"] = _paired(outs, base)
            results[name][f"held_out_vs_{ref}"] = _paired(
                [o for o in outs if o.family == HELD_OUT_FAMILY.value], base_held
            )
    return {
        "source_report": str(report_path).replace("\\", "/"),
        "backend": {
            "name": backend.name,
            "small": backend.model_id(Tier.SMALL),
            "large": backend.model_id(Tier.LARGE),
        },
        "n_final_per_family": n_final,
        "task_seed_offset": task_seed_offset,
        "final_seed": SPLIT_SEEDS["final"] + task_seed_offset,
        "tasks": len(cases),
        "arms": results,
    }


def _cmp(p: dict[str, Any], h: dict[str, Any]) -> str:
    return (
        f"{p['wins']}/{p['losses']}/{p['ties']} | {p['accuracy_delta']:+.3f} "
        f"({p['accuracy_delta_ci95'][0]:+.3f}, {p['accuracy_delta_ci95'][1]:+.3f}) | "
        f"{p['mcnemar_exact_p']:.3g} | {h['accuracy_delta']:+.3f} ({h['mcnemar_exact_p']:.3g})"
    )


def _pct(summary: dict[str, Any]) -> str:
    if not summary.get("tasks"):
        return "– (0)"
    return f"{100 * summary['accuracy']:.1f}% ({summary['tasks']})"


def render_confirm(result: dict[str, Any]) -> str:
    lines = [
        "# Confirmation on fresh final tasks",
        "",
        (
            f"Frozen harnesses from `{result['source_report']}` (hashes verified), evaluated once on "
            f"{result['tasks']} new final-split tasks (seed {result['final_seed']}, "
            f"{result['n_final_per_family']} per family including the held-out family). "
            f"Backend `{result['backend']['name']}` (small `{result['backend']['small']}`, "
            f"large `{result['backend']['large']}`)."
        ),
        "",
        (
            "| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) "
            "| McNemar p | Held-out Δ (p) |"
        ),
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for name, r in result["arms"].items():
        o = r["overall"]
        lo, hi = r["accuracy_ci95"]
        cmp = _cmp(r["vs_fixed"], r["held_out_vs_fixed"]) if "vs_fixed" in r else "– | – | – | –"
        lines.append(
            f"| {name} | {100 * o['accuracy']:.1f}% ({100 * lo:.1f}–{100 * hi:.1f}) | "
            f"{100 * r['held_out_family']['accuracy']:.1f}% | {cmp} |"
        )
    variants = {n: r for n, r in result["arms"].items() if "vs_ratchet" in r}
    if variants:
        lines += [
            "",
            "## Variants vs the frozen ratchet harness",
            "",
            "| Variant | vs ratchet W/L/T | Δ (95% CI) | McNemar p | Held-out Δ (p) |",
            "|---|---|---:|---:|---:|",
        ]
        for name, r in variants.items():
            lines.append(f"| {name} | {_cmp(r['vs_ratchet'], r['held_out_vs_ratchet'])} |")
    lines += [
        "",
        "## Where the accuracy comes from, and what it cost",
        "",
        (
            "Compiled tasks are answered by a zero-model recipe; model-answered tasks went through "
            "the model (accuracy (n)). Wall time is measured end to end on this machine; GPU energy "
            "is mean sampled board power × wall time (nvidia-smi, best effort)."
        ),
        "",
        (
            "| Harness | Compiled | Model-answered | Model calls | Large-tier calls | Wall s "
            "| s / verified completion | Wh / verified completion | Proxy USD |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, r in result["arms"].items():
        o = r["overall"]
        spc = r.get("seconds_per_verified_completion")
        wpc = (r.get("gpu_power") or {}).get("wh_per_verified_completion")
        spc_s = f"{spc:.1f}" if spc else "–"
        wpc_s = f"{wpc:.3f}" if wpc else "–"
        lines.append(
            f"| {name} | {_pct(r.get('compiled_tasks', {}))} | "
            f"{_pct(r.get('model_answered_tasks', {}))} | {o['model_calls']} | "
            f"{o['large_tier_calls']} | {r.get('wall_seconds', 0):.0f} | {spc_s} | {wpc_s} | "
            f"${o['cost_usd']:.4f} |"
        )
    lines += [
        "",
        (
            "`ratchet@small_only` forces default routing to the small model; `ratchet@checker_*` "
            "adds an independent large-tier checker role (no tools) to the frozen ratchet harness."
        ),
    ]
    return "\n".join(lines) + "\n"


# -- price sensitivity ---------------------------------------------------------------
# Illustrative two-tier price sheets in USD per million (input, output) tokens. They are
# generic tiers spanning cheap-to-expensive hosted pricing, not quotes from any provider.
PRICE_SHEETS: dict[str, dict[str, tuple[float, float]]] = {
    "published_default": {"small": (0.10, 0.40), "large": (0.60, 2.40)},
    "both_cheap": {"small": (0.05, 0.20), "large": (0.20, 0.80)},
    "narrow_gap": {"small": (0.30, 1.20), "large": (0.60, 2.40)},
    "wide_gap": {"small": (0.10, 0.40), "large": (3.00, 15.00)},
}


def reprice(report_path: Path, sheets: dict[str, dict[str, tuple[float, float]]] | None = None):
    """Recompute final-split cost per verified completion under several price sheets."""
    sheets = sheets or PRICE_SHEETS
    report = json.loads(report_path.read_text(encoding="utf-8"))
    tiers = {report["config"]["small_model"]: "small", report["config"]["large_model"]: "large"}
    arms: dict[str, Any] = {}
    for arm, data in report["final"]["arms"].items():
        if "model_ledger" not in data:  # paired-comparison entries share this mapping
            continue
        tokens = {"small": [0, 0], "large": [0, 0]}
        for key, entry in data["model_ledger"].items():
            if not key.startswith("role:"):
                continue
            model_id = key.split("/", 1)[1]  # "role:<role>/<model_id>"
            model = model_id.removeprefix("ollama/")
            tier = tiers.get(model) or ("small" if "-small-" in model_id else "large")
            tokens[tier][0] += entry["input_tokens"]
            tokens[tier][1] += entry["output_tokens"]
        completions = data["overall"]["verified_completions"]
        costs = {}
        for name, sheet in sheets.items():
            usd = sum(
                (tokens[t][0] * sheet[t][0] + tokens[t][1] * sheet[t][1]) / 1_000_000
                for t in ("small", "large")
            )
            costs[name] = {
                "usd": usd,
                "per_verified_completion": usd / completions if completions else None,
            }
        arms[arm] = {"tokens": tokens, "verified_completions": completions, "costs": costs}
    ratios = {}
    if "fixed" in arms and "ratchet" in arms:
        for name in sheets:
            a = arms["ratchet"]["costs"][name]["per_verified_completion"]
            b = arms["fixed"]["costs"][name]["per_verified_completion"]
            ratios[name] = a / b if a and b else None
    return {
        "source_report": str(report_path).replace("\\", "/"),
        "sheets": {k: {t: list(v) for t, v in s.items()} for k, s in sheets.items()},
        "arms": arms,
        "ratchet_over_fixed_cost_per_completion": ratios,
    }


def render_reprice(result: dict[str, Any]) -> str:
    sheets = list(result["sheets"])
    lines = [
        "# Price sensitivity of cost per verified completion",
        "",
        (
            f"Measured final-split token counts from `{result['source_report']}`, re-priced under "
            "illustrative two-tier sheets (USD per million input/output tokens; generic tiers, "
            "not provider quotes): "
            + "; ".join(
                f"`{k}` small {v['small'][0]}/{v['small'][1]}, large {v['large'][0]}/{v['large'][1]}"
                for k, v in result["sheets"].items()
            )
            + "."
        ),
        "",
        "| Arm | Verified completions | " + " | ".join(f"`{s}`" for s in sheets) + " |",
        "|---|---:|" + "---:|" * len(sheets),
    ]
    for arm, r in result["arms"].items():
        cells = []
        for s in sheets:
            v = r["costs"][s]["per_verified_completion"]
            cells.append(f"${v:.5f}" if v else "–")
        lines.append(f"| {arm} | {r['verified_completions']} | " + " | ".join(cells) + " |")
    ratios = result["ratchet_over_fixed_cost_per_completion"]
    if ratios:
        lines.append(
            "| **ratchet ÷ fixed** | | "
            + " | ".join(f"**{ratios[s]:.2f}×**" if ratios[s] else "–" for s in sheets)
            + " |"
        )
    return "\n".join(lines) + "\n"


# -- pre-registration ------------------------------------------------------------------
def commitment_digest(offset: int, salt: str) -> str:
    return hashlib.sha256(f"rasis-v3-commit|offset={offset}|salt={salt}".encode()).hexdigest()


def verify_commitment(path: Path, offset: int, salt: str) -> bool:
    record = json.loads(path.read_text(encoding="utf-8"))
    return record["commitment"] == commitment_digest(offset, salt)
