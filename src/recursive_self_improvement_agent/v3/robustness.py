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
import json
import statistics
from pathlib import Path
from typing import Any

from .harness import HarnessConfig, RoutingPolicy, Tier, harness_hash
from .models import ModelCache
from .registry import Registry
from .stats import wilson
from .study import ArmLedger, Evaluator, Study, StudyConfig, _paired, make_backend
from .tasks import HELD_OUT_FAMILY, SPLIT_SEEDS, build_split
from .verifier import summarize


def sweep(
    seeds: list[int],
    guide_effects: list[float],
    arms: tuple[str, ...],
    base: StudyConfig | None = None,
) -> dict[str, Any]:
    base = base or StudyConfig()
    runs = []
    for effect in guide_effects:
        for seed in seeds:
            config = dataclasses.replace(
                base,
                provider="simulated",
                seed=seed,
                task_seed_offset=1000 * seed,
                guide_effect=effect,
                arms=arms,
                approval="standing",
                audits=False,
            )
            final = Study(config, Registry()).run()["final"]["arms"]
            row: dict[str, Any] = {"guide_effect": effect, "seed": seed}
            for arm in arms:
                row[arm] = final[arm]["overall"]["accuracy"]
                row[f"{arm}_held_out"] = final[arm]["held_out_family"]["accuracy"]
            if "ratchet_vs_prompt_only" in final:
                p = final["ratchet_vs_prompt_only"]
                row["ratchet_minus_prompt_only"] = p["accuracy_delta"]
                row["ratchet_vs_prompt_only_p"] = p["mcnemar_exact_p"]
            runs.append(row)
    summary = {}
    for effect in guide_effects:
        rows = [r for r in runs if r["guide_effect"] == effect]
        entry: dict[str, Any] = {}
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
        summary[str(effect)] = entry
    return {
        "seeds": seeds,
        "guide_effects": guide_effects,
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
            f"Seeds {result['seeds']} (each seed shifts every split seed by 1000×seed and reseeds the simulator); "
            "guide effect = multiplier on a failure mode's probability when the matching advisory guide is present "
            "(1.0 = guides do nothing, 0.6 = the published default, 0.2 = guides nearly as strong as structural fixes). "
            "Sealed final accuracy across seeds: mean [min–max]."
        ),
        "",
        "| Guide effect | " + " | ".join(arms) + " | Ratchet > prompt-only (p<0.05) |",
        "|---:|" + "---:|" * len(arms) + "---:|",
    ]
    for effect, entry in result["summary"].items():
        cells = [
            f"{100 * entry[a]['mean']:.1f}% [{100 * entry[a]['min']:.0f}–{100 * entry[a]['max']:.0f}]"
            for a in arms
        ]
        wins = f"{entry.get('ratchet_beats_prompt_only_p_lt_0.05', 'n/a')}/{entry.get('seeds', '')}"
        lines.append(f"| {effect} | " + " | ".join(cells) + f" | {wins} |")
    return "\n".join(lines) + "\n"


def confirm(
    report_path: Path, arms: list[str], n_final: int, task_seed_offset: int
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fields = {f.name for f in dataclasses.fields(StudyConfig)}
    stored = {k: v for k, v in report["config"].items() if k in fields}
    stored["arms"] = tuple(stored.get("arms", ()))
    config = StudyConfig(**{**stored, "n_final": n_final, "task_seed_offset": task_seed_offset})
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
        harnesses["ratchet@small_only"] = harnesses["ratchet"].model_copy(
            update={"routing": RoutingPolicy(default_tier=Tier.SMALL)}
        )
    outcomes = {}
    results: dict[str, Any] = {}
    for name, harness in harnesses.items():
        outs = Evaluator(backend, ModelCache(), ArmLedger()).run(cases, harness)[1]
        outcomes[name] = outs
        held = [o for o in outs if o.family == HELD_OUT_FAMILY.value]
        results[name] = {
            "harness_hash": harness_hash(harness),
            "overall": summarize(outs),
            "accuracy_ci95": wilson(sum(o.correct for o in outs), len(outs)),
            "held_out_family": summarize(held),
        }
    if "fixed" in outcomes:
        base = outcomes["fixed"]
        base_held = [o for o in base if o.family == HELD_OUT_FAMILY.value]
        for name, outs in outcomes.items():
            if name != "fixed":
                results[name]["vs_fixed"] = _paired(outs, base)
                results[name]["held_out_vs_fixed"] = _paired(
                    [o for o in outs if o.family == HELD_OUT_FAMILY.value], base_held
                )
    return {
        "source_report": str(report_path),
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


def render_confirm(result: dict[str, Any]) -> str:
    lines = [
        "# Confirmation on fresh final tasks",
        "",
        (
            f"Frozen harnesses from `{result['source_report']}` (hashes verified), evaluated once on "
            f"{result['tasks']} new final-split tasks (seed {result['final_seed']}, {result['n_final_per_family']} per family "
            f"including the held-out family). Backend `{result['backend']['name']}` "
            f"({result['backend']['small']}, {result['backend']['large']})."
        ),
        "",
        "| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) | McNemar p | Held-out Δ (p) | Cost |",
        "|---|---:|---:|---|---:|---:|---:|---:|",
    ]
    for name, r in result["arms"].items():
        o = r["overall"]
        lo, hi = r["accuracy_ci95"]
        if "vs_fixed" in r:
            p, h = r["vs_fixed"], r["held_out_vs_fixed"]
            cmp = (
                f"{p['wins']}/{p['losses']}/{p['ties']} | {p['accuracy_delta']:+.3f} "
                f"({p['accuracy_delta_ci95'][0]:+.3f}, {p['accuracy_delta_ci95'][1]:+.3f}) | {p['mcnemar_exact_p']:.3g} | "
                f"{h['accuracy_delta']:+.3f} ({h['mcnemar_exact_p']:.3g})"
            )
        else:
            cmp = "– | – | – | –"
        lines.append(
            f"| {name} | {100 * o['accuracy']:.1f}% ({100 * lo:.1f}–{100 * hi:.1f}) | "
            f"{100 * r['held_out_family']['accuracy']:.1f}% | {cmp} | ${o['cost_usd']:.4f} |"
        )
    lines += [
        "",
        (
            "`ratchet@small_only` is the final ratchet harness with default routing forced to the small "
            "model, isolating the harness effect from the proposer's choice to route to the large model."
        ),
    ]
    return "\n".join(lines) + "\n"
