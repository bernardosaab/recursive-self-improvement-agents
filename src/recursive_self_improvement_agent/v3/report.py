"""Render a v3 study report as a readable Markdown summary."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.6f}"


def render(report: dict[str, Any]) -> str:
    lines = [
        "# v3 harness-ratchet study summary",
        "",
        (
            f"Backend: `{report['backend']['name']}` (small `{report['backend']['small']}`, large `{report['backend']['large']}`). "
            f"Generator `{report['generator']}`, seeds {report['split_seeds']}."
        ),
        "",
        "## Sealed final split (frozen harness per arm; evaluated once)",
        "",
        "| Arm | Accuracy (95% CI) | Held-out family | Verified completions | Cost / verified completion | Unsupported claims | Wrong writes | Blocked attempts | Executed violations | Model calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, r in report["final"]["arms"].items():
        if "overall" not in r:
            continue
        o, h = r["overall"], r["held_out_family"]
        lo, hi = r["accuracy_ci95"]
        lines.append(
            f"| {arm} | {_pct(o['accuracy'])} ({_pct(lo)}–{_pct(hi)}) | {_pct(h['accuracy'])} | {o['verified_completions']} | "
            f"{_usd(o['cost_per_verified_completion'])} | {o['unsupported_claims']} | {o['wrong_writes']} | {o['blocked_attempts']} | "
            f"{o['executed_violations']} | {o['model_calls']} |"
        )
    lines += [
        "",
        "### Paired comparisons on the final split",
        "",
        "| Comparison | W/L/T | Δ accuracy (95% bootstrap CI) | McNemar exact p |",
        "|---|---|---:|---:|",
    ]
    for arm, r in report["final"]["arms"].items():
        for key, label in (
            ("vs_fixed", f"{arm} vs fixed"),
            ("held_out_vs_fixed", f"{arm} vs fixed (held-out family)"),
        ):
            if key in r:
                p = r[key]
                lines.append(
                    f"| {label} | {p['wins']}/{p['losses']}/{p['ties']} | {p['accuracy_delta']:+.3f} ({p['accuracy_delta_ci95'][0]:+.3f}, {p['accuracy_delta_ci95'][1]:+.3f}) | {p['mcnemar_exact_p']:.3g} |"
                )
    if "ratchet_vs_prompt_only" in report["final"]["arms"]:
        p = report["final"]["arms"]["ratchet_vs_prompt_only"]
        lines.append(
            f"| ratchet vs prompt_only | {p['wins']}/{p['losses']}/{p['ties']} | {p['accuracy_delta']:+.3f} ({p['accuracy_delta_ci95'][0]:+.3f}, {p['accuracy_delta_ci95'][1]:+.3f}) | {p['mcnemar_exact_p']:.3g} |"
        )

    lines += [
        "",
        "## Improvement trajectory and budget per arm",
        "",
        "| Arm | Cycle outcomes | Promotions | Candidates evaluated | Selection pairs | Live model calls | Reused (replay) calls | Proposer calls | Ratchet entries |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, r in report["arms"].items():
        outcomes = [c.get("outcome", c.get("stop", "")) for c in r["cycles"]]
        promos = sum(o == "promoted" for o in outcomes)
        led = r["ledger"]
        lines.append(
            f"| {arm} | {'; '.join(outcomes) or 'no tuning'} | {promos} | {led['candidate_evaluations']} | {led['selection_pairs']} | "
            f"{led['live_model_calls']} | {led['reused_model_calls']} | {led['proposer'].get('calls', 0) if led['proposer'] else 0} | {len(r['ratchet_suite'])} |"
        )
    rep = report["arms"].get("ratchet", {}).get("ledger", {}).get("replay")
    if rep:
        lines += [
            "",
            f"Replay (ratchet arm): {rep['in_support_tasks']} development task runs replayed exactly from logged calls, {rep['out_of_support_tasks']} needed live execution; live re-check agreement {rep['agreement_matched']}/{rep['agreement_checked']}.",
        ]

    f = report["factorial"]
    lines += [
        "",
        "## Harness × model factorial (final split)",
        "",
        "| Cell | Accuracy | Cost | Large-tier calls |",
        "|---|---:|---:|---:|",
    ]
    for cell, v in f["cells"].items():
        lines.append(
            f"| {cell} | {_pct(v['accuracy'])} | {_usd(v['cost_usd'])} | {v['large_tier_calls']} |"
        )
    lines += [
        "",
        f"Harness effect with small model: {f['harness_effect_small']:+.3f}; with large model: {f['harness_effect_large']:+.3f}. Model effect on baseline harness: {f['model_effect_baseline']:+.3f}; on ratchet harness: {f['model_effect_ratchet']:+.3f}.",
    ]

    for arm, attr in report.get("attribution", {}).items():
        if not attr.get("edits"):
            continue
        fv = attr["full_vs_baseline"]
        lines += [
            "",
            f"## Interventional attribution of promoted edits ({arm})",
            "",
            (
                "Provenance (the registry) says which edit was promoted, by which proposer, under "
                "which review. Attribution says what each edit *caused*. Each edit is removed from "
                "the final harness (leave-one-out) or applied alone to the baseline (add-one), and "
                "the variants run on the identical sealed final tasks."
            ),
            "",
            (
                f"Full harness vs baseline: {fv['accuracy_delta']:+.3f} "
                f"({fv['accuracy_delta_ci95'][0]:+.3f}, {fv['accuracy_delta_ci95'][1]:+.3f})."
            ),
            "",
            "| Cycle | Source | Edit | Leave-one-out effect (95% CI) | Add-one effect (95% CI) | Interaction | Review event |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
        for e in attr["edits"]:
            ops = "; ".join(
                o["op"] + ":" + str(next((v for k, v in o.items() if k != "op"), ""))
                for o in e["ops"]
            )
            if not e["separable"]:
                lines.append(
                    f"| {e['cycle']} | {e['source']} | {ops} | not separable | | | {e['review_seq']} |"
                )
                continue
            lo, ad = e["leave_one_out"], e["add_one"]
            lines.append(
                f"| {e['cycle']} | {e['source']} | {ops} | {lo['accuracy_delta']:+.3f} "
                f"({lo['accuracy_delta_ci95'][0]:+.3f}, {lo['accuracy_delta_ci95'][1]:+.3f}) | "
                f"{ad['accuracy_delta']:+.3f} ({ad['accuracy_delta_ci95'][0]:+.3f}, "
                f"{ad['accuracy_delta_ci95'][1]:+.3f}) | {e['interaction']:+.3f} | {e['review_seq']} |"
            )

    lines += ["", "## Model ledger on the final split (which model did what)", ""]
    lines += [
        "| Arm | Role / model | Calls | Tokens in | Tokens out | Cost |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for arm, r in report["final"]["arms"].items():
        for key, row in r.get("model_ledger", {}).items():
            if key.startswith("role:"):
                lines.append(
                    f"| {arm} | {key[5:]} | {row['calls']} | {row['input_tokens']} | "
                    f"{row['output_tokens']} | {_usd(row['cost_usd'])} |"
                )
    teams = {
        a: r["team"] for a, r in report["final"]["arms"].items() if r.get("team", {}).get("reviews")
    }
    if teams:
        lines += [
            "",
            "Checker reviews on the final split: "
            + "; ".join(
                f"{a}: {t['reviews']} reviews, {t['objections']} objections, "
                f"{t['unresolved_abstentions']} unresolved abstentions"
                for a, t in teams.items()
            )
            + ".",
        ]

    lines += [
        "",
        "## Adversarial audits",
        "",
        "| Honeypot | Rejected | Stage | Detail |",
        "|---|---|---|---|",
    ]
    for h in report["honeypots"]:
        detail = h.get("reason") or ", ".join(h.get("failed_checks", []))
        lines.append(f"| {h['honeypot']} | {h['rejected']} | {h['stage']} | {detail} |")
    c = report["chaos_write_gate_offline"]
    lines += [
        "",
        f"Chaos test (write gate offline): {c['write_attempts']} write attempts, {c['fail_closed']} failed closed, {c['executed_writes']} executed. Passes: {c['passes']}.",
        "",
        f"Registry hash chain valid: {report['registry_chain_valid']}.",
        "",
    ]
    return "\n".join(lines)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
