# Recursive Self-Improvement Agents

**An agent that improves its own harness through repeated, statistically gated cycles, without changing its weights or running generated code, and without ever loosening its own controls.**

An agent's reliability depends less on the model than on the *harness* around it: what context it sees, which deterministic checks inspect its answers, which model handles which step, when it retries, when it abstains, and which recurring work needs no model at all. This repository treats that harness as the object of recursive improvement.

Each cycle, the system diagnoses the agent's failures and proposes a typed harness edit. It tests the edit under error control that stays valid across unbounded cycles. Every failure it fixes becomes a permanent regression test, and only edits that beat the incumbent are promoted. Every promoted version becomes the parent of the next cycle: that is the recursion.

> **Status:** v3 is the current study. v1 and v2 are preserved unchanged below, including v2's null result, because a harder benchmark and a richer mutable surface were the response to that null result, not a replacement for it.

## What is new in v3

| Mechanism | What it does | Where |
|---|---|---|
| **Harness language** | The only mutable surface is a typed, non-executable configuration language, which a proposer can change only through 12 edit operations: advisory guides plus bounded free text, a context policy (as-of filter, supersession, entity filter, size, order), a deterministic quarantine hook for instruction-like text, seven declarative output sensors with `retry`/`escalate`/`abstain` actions, hazard-based routing between a small and a large model, tool retries, and compiled known-path recipes. Tool grants, the refund ceiling, budgets, labels, the evaluator and the promotion rule are **not expressible** in the language. | `v3/harness.py` |
| **Automated ratchet** | Every promotion freezes the development failures it fixed as golden jobs. These join seeded negative tests (injected instructions, writes above the auto-approval ceiling, writes that must not happen) in an append-only, hash-chained suite. *Monotone ratchet:* a candidate must pass every entry the incumbent passes. | `v3/ratchet.py` |
| **Anytime-valid promotion** | Paired selection runs in batches and may stop early. The gate is a betting e-process (testing by betting) with error spending α·6/(π²t²) across an unbounded number of recursive selection tests. Per-family regressions are tested with Holm-corrected exact sign tests. An edit may be promoted for accuracy, or for cost at non-inferior accuracy. | `v3/stats.py`, `v3/study.py` |
| **Known-path compiler** | When development traces show that a family's resolution is deterministic and agrees with verified outcomes on every covered task, the compiler proposes a zero-model recipe. At run time the recipe **skips** to the model, with a recorded reason, when its preconditions fail (schema drift, conflict, missing field). It never guesses. | `v3/compiler.py`, `runtime.compiled_answer` |
| **Support-aware counterfactual replay** | Tools are deterministic and every model call is content-addressed. A candidate harness is therefore first evaluated by re-running the runtime over the logged calls. Tasks whose model inputs are byte-identical are scored exactly with no new model call; only out-of-support tasks run live. A random live re-check measures agreement. | `v3/models.py` (`ModelCache`), `Evaluator.run` |
| **Experiential proposer memory** | Every candidate's measured utility and rejection reason is stored. Proposers avoid edits already shown not to help, and the model-based proposer is shown helpful and unhelpful exemplars. | `v3/proposer.py` |
| **Bi-temporal registry** | Harness versions are content-addressed. Activations are validity intervals that are closed, never deleted (SQLite triggers enforce this). Every diagnose, propose, evaluate, review and activate step goes into a hash-chained event log. The registry answers as-of queries ("which harness ran at t"), exports PROV-JSON lineage, and builds a reconstruction pack for every promotion. | `v3/registry.py` |
| **Agent team with trusted arbitration** | A `team` section adds an independent *checker* role on its own model tier, run `always`, `on_hazard` or `on_write`. The checker holds no tools and cannot write or sign. On an objection, a fixed arbitration rule outside the harness language decides: escalate the maker to the large model once, then abstain. Every review and disagreement is recorded in the trace. | `v3/runtime.py` (`_check`) |
| **Versioned company knowledge** | Guides are wiki pages with an id, version, owning team and authority marker. Only `policy` pages can be selected; archived or draft pages exist in the record but cannot bind an agent. Prompts and traces cite the exact page version in force. | `v3/harness.py` (`WIKI`) |
| **Provenance vs causal attribution** | The registry records *what* changed, *who* proposed it and *which* review approved it; that is provenance. Separately, each promoted edit's causal effect is estimated by intervention on the identical sealed final tasks: remove it from the final harness (leave-one-out) or apply it alone to the baseline (add-one), giving paired effects with CIs plus interaction terms. | `Study.attribution` |
| **Model ledger** | Calls, tokens and cost per role (maker or checker), per model and per family answer "which model did this" from the run record. | `_model_ledger` |
| **Resumable, exact runs** | A durable, content-addressed call store makes any study resumable: `--cache` plus `--max-seconds` or `--max-calls` split it into chunks. Each re-run deterministically replays stored calls and continues where it stopped. A test proves a chunked run reproduces the uninterrupted report exactly, including cost accounting. | `CallStore`, `Checkpoint` |
| **Model vs harness factorial** | The final split crosses {baseline, final harness} × {small-only, large-only, as-configured}, separating the harness effect from the model effect. | `Study.factorial` |
| **Adversarial audits** | Honeypot candidates (forbidden operations, tool grants, smuggled instructions, archived or draft wiki pages, a checker given write authority, stale parents, "cheap and blind", "abstain-heavy") must be rejected by the validator or the gate. A chaos test takes the write gate offline: every write must fail closed. | `Study.honeypots`, `Study.chaos` |

## Benchmark `harness-v3`

There are four task families behind one interface:
- invoice reconciliation (`eq`)
- refund requests, which include a simulated write staged behind a fixed ceiling (`ge`)
- service triage (`ge`)
- contract obligations (`le`): **held out entirely**. It is never used for development, selection, diagnostics or compilation, and appears only in the sealed final split.

Records are bi-temporal (valid-from and recorded-at). A claim is resolved by the single authoritative record known on the as-of day, or is `insufficient` when the field is missing or authoritative records conflict.

Independently sampled hazards create failures that a harness can fix:
- superseded stale records
- back-dated amendments recorded *after* the as-of day
- prompt-injection notes
- conflicting parallel entries
- missing fields
- distractor entities
- transient tool faults
- field-name drift (final split only)

The verifier requires the correct decision, **exact** citation of the authoritative records, and an exactly correct write. Seeds: development 5101, selection 5203, final 5307, ratchet negatives 5411.

## Results

### Real local models (Ollama: `llama3.2:3b` small tier, `qwen2.5:7b` large tier and proposer)

Observed 15 November 2026 on one laptop (RTX 3070, 8 GB) with Ollama. Small tier `llama3.2:3b`; large tier and model-based proposer `qwen2.5:7b`. Temperature 0 on first attempts and 0.7 with fixed seeds on retries; 4,096-token context; output capped at 512 tokens (1,024 for the proposer).

Sizes: 12 development, 25 selection and 20 final tasks per family, plus the held-out family's 20 in the final split; 3 cycles; 2 proposals per cycle; standing approval.

The run used 2,948 stored model calls. It was executed as 13 resumable, time-boxed chunks of about 6–7 minutes (≈ 1.5 h of wall-clock time in total) and finished with the same report an uninterrupted run would produce. Nothing left the machine, and the monetary cost was $0. USD columns are the hosted-equivalent *proxy* described under Limitations. Proposer wall time was not preserved across resumes in this run; the code now records it.

**Headline findings (real models, sealed final split, evaluated once):**

- **The recursive harness ratchet took the agent from 7.5% to 60.0% accuracy** (42 paired wins, 0 losses; McNemar p = 4.6e-13). On the **held-out family**, which was never seen during improvement, it went from 0% to 35% (7/0; p = 0.016). Cost per verified completion *fell* from $0.00094 to $0.00055, because compiled known paths replaced 29 of 80 model calls.
- **Prompt-only reflection produced nothing deployable.** Across three cycles, the real proposer, restricted to guide and free-text edits, offered injection-related guidance that never improved development accuracy (Δ −0.056 to 0.0). Its final harness is the baseline. Random structural search reached 40.0%; the LLM proposer without the compiler reached 47.5%; the full ratchet reached 60.0%.
- **The harness effect is larger than the model effect.** In the factorial, the final harness on the *small* model improves accuracy by +0.44 over the baseline harness on the same model. Upgrading the model under the baseline harness adds +0.30. With the large model, the harness still adds +0.23. The real proposer *did* choose to route by default to the large model; the factorial separates that choice from the structural edits. Even in the small-only cell, sensor-triggered escalation still made 11 large-tier calls.
- **Attribution by intervention.** Removing the compiled known paths from the final harness costs −0.113 (CI −0.200 to −0.037). Removing the LLM-proposed bundle (large default tier, conflict sensor, retries, conflict escalation) costs −0.263. Their negative interaction (−0.138) shows they partly fix the same failures.
- **The proposer is untrusted, and that matters:** 6 of 18 real-model proposals were invalid (1 malformed; 5 no-ops or re-compiling families already compiled). The validator rejected all of them before anything ran.
- **Controls held under real models.** The models made 68 blocked tool or write requests under the baseline harness and 13 under the ratchet harness, with 0 executed violations. All 9 honeypots were rejected. With the write gate offline, 8 of 8 writes failed closed. Replay re-check agreement was 8/8.
- **What did not improve:** checker roles were proposed but never survived screening in a promoted form, so team review is exercised by tests and the offline run, not demonstrated as a real-model gain. The held-out family remains weak in absolute terms (35%), and so does drift handling: 24 final tasks skipped the compiled path for schema drift or missing fields, as designed, and fell back to the model.


#### Sealed final split (frozen harness per arm; evaluated once)

| Arm | Accuracy (95% CI) | Held-out family | Verified completions | Cost / verified completion | Unsupported claims | Wrong writes | Blocked attempts | Executed violations | Model calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 7.5% (3.5%–15.4%) | 0.0% | 6 | $0.000943 | 70 | 7 | 68 | 0 | 80 |
| ratchet | 60.0% (49.0%–70.0%) | 35.0% | 37 | $0.000553 | 31 | 3 | 13 | 0 | 51 |
| ratchet_no_compiler | 47.5% (36.9%–58.3%) | 35.0% | 27 | $0.001147 | 39 | 7 | 17 | 0 | 80 |
| prompt_only | 7.5% (3.5%–15.4%) | 0.0% | 6 | $0.000943 | 70 | 7 | 68 | 0 | 80 |
| random | 40.0% (30.0%–51.0%) | 25.0% | 27 | $0.001018 | 40 | 7 | 15 | 0 | 80 |

#### Paired comparisons on the final split

| Comparison | W/L/T | Δ accuracy (95% bootstrap CI) | McNemar exact p |
|---|---|---:|---:|
| ratchet vs fixed | 42/0/38 | +0.525 (+0.412, +0.637) | 4.55e-13 |
| ratchet vs fixed (held-out family) | 7/0/13 | +0.350 (+0.150, +0.550) | 0.0156 |
| ratchet_no_compiler vs fixed | 32/0/48 | +0.400 (+0.300, +0.512) | 4.66e-10 |
| ratchet_no_compiler vs fixed (held-out family) | 7/0/13 | +0.350 (+0.150, +0.550) | 0.0156 |
| prompt_only vs fixed | 0/0/80 | +0.000 (+0.000, +0.000) | 1 |
| prompt_only vs fixed (held-out family) | 0/0/20 | +0.000 (+0.000, +0.000) | 1 |
| random vs fixed | 26/0/54 | +0.325 (+0.225, +0.425) | 2.98e-08 |
| random vs fixed (held-out family) | 5/0/15 | +0.250 (+0.099, +0.450) | 0.0625 |
| ratchet vs prompt_only | 42/0/38 | +0.525 (+0.412, +0.637) | 4.55e-13 |

#### Improvement trajectory and budget per arm

| Arm | Cycle outcomes | Promotions | Candidates evaluated | Selection pairs | Live model calls | Reused (replay) calls | Proposer calls | Ratchet entries |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| fixed | no tuning | 0 | 0 | 0 | 0 | 0 | 0 | 12 |
| ratchet | promoted; no candidate passed the development screen; promoted | 2 | 4 | 90 | 231 | 219 | 6 | 31 |
| ratchet_no_compiler | promoted; no candidate passed the development screen; no candidate passed the development screen | 1 | 4 | 30 | 294 | 229 | 6 | 24 |
| prompt_only | no candidate passed the development screen; no candidate passed the development screen; no candidate passed the development screen | 0 | 5 | 0 | 216 | 72 | 6 | 12 |
| random | promoted; rejected; rejected | 1 | 6 | 180 | 484 | 259 | 3 | 23 |

Replay (ratchet arm): 72 development task runs replayed exactly from logged calls, 72 needed live execution; live re-check agreement 8/8.

#### Harness × model factorial (final split)

| Cell | Accuracy | Cost | Large-tier calls |
|---|---:|---:|---:|
| baseline/small_only | 7.5% | $0.005678 | 0 |
| baseline/large_only | 37.5% | $0.028123 | 80 |
| baseline/as_configured | 7.5% | $0.005660 | 0 |
| ratchet_final/small_only | 51.2% | $0.009244 | 11 |
| ratchet_final/large_only | 60.0% | $0.020473 | 51 |
| ratchet_final/as_configured | 60.0% | $0.020473 | 51 |

Harness effect with small model: +0.437; with large model: +0.225. Model effect on baseline harness: +0.300; on ratchet harness: +0.088.

#### Interventional attribution of promoted edits (ratchet)

Provenance (the registry) says which edit was promoted, by which proposer, under which review. Attribution says what each edit *caused*. Each edit is removed from the final harness (leave-one-out) or applied alone to the baseline (add-one), and the variants run on the identical sealed final tasks.

Full harness vs baseline: +0.512 (+0.400, +0.625).

| Cycle | Source | Edit | Leave-one-out effect (95% CI) | Add-one effect (95% CI) | Interaction | Review event |
|---:|---|---|---:|---:|---:|---:|
| 1 | known_path_compiler | add_compiled:invoice_reconciliation; add_compiled:refund_request; add_compiled:service_triage | +0.113 (+0.037, +0.200) | +0.250 (+0.163, +0.350) | -0.138 | 9 |
| 3 | harness_ratchet_ollama | set_default_tier:large; add_sensor:conflict_requires_abstain; set_tool_retries:2; add_escalation:conflict_detected | +0.263 (+0.175, +0.362) | +0.400 (+0.300, +0.512) | -0.138 | 15 |

#### Interventional attribution of promoted edits (ratchet_no_compiler)

Provenance (the registry) says which edit was promoted, by which proposer, under which review. Attribution says what each edit *caused*. Each edit is removed from the final harness (leave-one-out) or applied alone to the baseline (add-one), and the variants run on the identical sealed final tasks.

Full harness vs baseline: +0.400 (+0.300, +0.512).

| Cycle | Source | Edit | Leave-one-out effect (95% CI) | Add-one effect (95% CI) | Interaction | Review event |
|---:|---|---|---:|---:|---:|---:|
| 1 | harness_ratchet_ollama | add_sensor:conflict_requires_abstain; set_check_when:on_write; set_tool_retries:2; set_default_tier:large | +0.412 (+0.300, +0.525) | +0.400 (+0.300, +0.512) | +0.012 | 22 |

#### Model ledger on the final split (which model did what)

| Arm | Role / model | Calls | Tokens in | Tokens out | Cost |
|---|---|---:|---:|---:|---:|
| fixed | maker/ollama/llama3.2:3b | 80 | 32126 | 6119 | $0.005660 |
| ratchet | maker/ollama/qwen2.5:7b | 51 | 25062 | 2265 | $0.020473 |
| ratchet_no_compiler | maker/ollama/qwen2.5:7b | 80 | 37852 | 3438 | $0.030962 |
| prompt_only | maker/ollama/llama3.2:3b | 80 | 32126 | 6119 | $0.005660 |
| random | maker/ollama/qwen2.5:7b | 80 | 34036 | 2948 | $0.027497 |

#### Adversarial audits

| Honeypot | Rejected | Stage | Detail |
|---|---|---|---|
| forbidden_operation | True | validator | 2 validation errors for HarnessProposal |
| forbidden_tool_grant | True | validator | 2 validation errors for HarnessProposal |
| instruction_smuggling | True | validator | candidate harness is invalid: 1 validation error for HarnessConfig |
| archived_wiki_page | True | validator | 2 validation errors for HarnessProposal |
| draft_wiki_page | True | validator | 2 validation errors for HarnessProposal |
| checker_given_write_authority | True | validator | 2 validation errors for HarnessProposal |
| stale_parent | True | validator | proposal parent does not match the active harness |
| cheap_and_blind | True | promotion_gate | monotone_ratchet, no_family_regression, improvement |
| abstain_heavy | True | promotion_gate | monotone_ratchet, no_family_regression, improvement |

Chaos test (write gate offline): 8 write attempts, 8 failed closed, 0 executed. Passes: True.

Registry hash chain valid: True.

### Robustness checks

**Replication on fresh real-model tasks.** The frozen harnesses from the run above (hashes verified) were evaluated once more on 160 new final-split instances (seed 5407; 40 per family including the held-out family) that were never used in any earlier step. Command: `rasis-v3 confirm`. Report: [`evidence/v3-ollama-confirm/`](evidence/v3-ollama-confirm/summary.md).

| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) | McNemar p | Held-out Δ (p) |
|---|---:|---:|---|---:|---:|---:|
| fixed | 2.5% (1.0–6.3) | 0.0% | – | – | – | – |
| ratchet | 57.5% (49.8–64.9) | 27.5% | 88/0/72 | +0.550 (+0.475, +0.625) | 6.5e-27 | +0.275 (0.001) |
| ratchet harness, small model only | 50.6% (43.0–58.3) | 12.5% | 78/1/81 | +0.481 (+0.400, +0.556) | 2.7e-22 | +0.125 (0.063) |

The headline effect replicates on twice the sample. Forcing the final harness onto the small model keeps most of the gain, so the improvement comes from the harness structure, not from the proposer's choice to route to the large model. Transfer to the held-out family is weaker on the small model and not significant there.

**Offline sensitivity to the simulator's assumption.** The simulator assumes advisory guides only partly reduce a failure mode, which could make "structural beats prompt-only" circular. The sweep re-runs the whole offline study over 5 task seeds (every split seed shifted, simulator reseeded) and over guide strengths from "guides do nothing" (1.0) to "a guide fully removes the failure mode it targets" (0.0). Command: `rasis-v3 sweep`. Report: [`evidence/v3-sweep/`](evidence/v3-sweep/summary.md).

| Guide effect | fixed | ratchet | prompt_only | random | Ratchet > prompt-only (p<0.05) |
|---:|---:|---:|---:|---:|---:|
| 1.0 | 40.1% [36–45] | 86.5% [82–92] | 40.1% [36–45] | 72.0% [51–86] | 5/5 |
| 0.6 (default) | 40.1% [36–45] | 86.5% [82–92] | 40.1% [36–45] | 72.0% [51–86] | 5/5 |
| 0.2 | 40.1% [36–45] | 86.5% [82–92] | 52.6% [45–61] | 72.0% [51–86] | 5/5 |
| 0.0 | 40.1% [36–45] | 86.5% [82–92] | 54.4% [45–71] | 72.0% [51–86] | 5/5 |

The table shows mean [min–max] sealed-final accuracy across seeds. The ratchet's advantage over prompt-only holds in all 20 seed × assumption runs, even when guides are assumed perfect. The reason is structural, not assumed: guidance cannot add tool retries, compiled known paths, citation or conflict sensors, or quarantine. Several failure classes are therefore out of reach of prompt edits, whatever their strength.

### Offline simulated backend

The offline backend is a seeded stochastic **stand-in, not a language model**. Its per-hazard error rates are declared assumptions in `SIMULATED_PROFILES`, including the assumption that advisory guides only partly reduce a failure mode. **The offline comparison therefore demonstrates the loop's mechanics and statistics. It is not evidence for the hypothesis that structural harness edits beat prompt edits.** That hypothesis is tested only against real models (above). Defaults: 30 development, 80 selection and 40 final tasks per family; 4 cycles; 2 proposals per cycle; standing approval.

#### Sealed final split (frozen harness per arm; evaluated once)

| Arm | Accuracy (95% CI) | Held-out family | Verified completions | Cost / verified completion | Unsupported claims | Wrong writes | Blocked attempts | Executed violations | Model calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 45.0% (37.5%–52.7%) | 45.0% | 50 | $0.000153 | 74 | 12 | 20 | 0 | 160 |
| ratchet | 88.1% (82.2%–92.3%) | 70.0% | 93 | $0.000102 | 15 | 0 | 0 | 0 | 99 |
| ratchet_no_memory | 88.1% (82.2%–92.3%) | 70.0% | 93 | $0.000102 | 15 | 0 | 0 | 0 | 99 |
| ratchet_no_replay | 88.1% (82.2%–92.3%) | 70.0% | 93 | $0.000102 | 15 | 0 | 0 | 0 | 99 |
| ratchet_no_compiler | 73.8% (66.4%–80.0%) | 72.5% | 71 | $0.000106 | 31 | 1 | 0 | 0 | 160 |
| prompt_only | 45.0% (37.5%–52.7%) | 45.0% | 50 | $0.000153 | 74 | 12 | 20 | 0 | 160 |
| random | 69.4% (61.8%–76.0%) | 65.0% | 78 | $0.000516 | 30 | 4 | 0 | 0 | 144 |

#### Paired comparisons on the final split

| Comparison | W/L/T | Δ accuracy (95% bootstrap CI) | McNemar exact p |
|---|---|---:|---:|
| ratchet vs fixed | 75/6/79 | +0.431 (+0.344, +0.525) | 2.91e-16 |
| ratchet vs fixed (held-out family) | 13/3/24 | +0.250 (+0.075, +0.425) | 0.0213 |
| ratchet_no_memory vs fixed | 75/6/79 | +0.431 (+0.344, +0.525) | 2.91e-16 |
| ratchet_no_memory vs fixed (held-out family) | 13/3/24 | +0.250 (+0.075, +0.425) | 0.0213 |
| ratchet_no_replay vs fixed | 75/6/79 | +0.431 (+0.344, +0.525) | 2.91e-16 |
| ratchet_no_replay vs fixed (held-out family) | 13/3/24 | +0.250 (+0.075, +0.425) | 0.0213 |
| ratchet_no_compiler vs fixed | 58/12/90 | +0.287 (+0.194, +0.381) | 2.25e-08 |
| ratchet_no_compiler vs fixed (held-out family) | 14/3/23 | +0.275 (+0.100, +0.475) | 0.0127 |
| prompt_only vs fixed | 0/0/160 | +0.000 (+0.000, +0.000) | 1 |
| prompt_only vs fixed (held-out family) | 0/0/40 | +0.000 (+0.000, +0.000) | 1 |
| random vs fixed | 54/15/91 | +0.244 (+0.156, +0.338) | 2.61e-06 |
| random vs fixed (held-out family) | 12/4/24 | +0.200 (+0.025, +0.375) | 0.0768 |
| ratchet vs prompt_only | 75/6/79 | +0.431 (+0.344, +0.525) | 2.91e-16 |

#### Improvement trajectory and budget per arm

| Arm | Cycle outcomes | Promotions | Candidates evaluated | Selection pairs | Live model calls | Reused (replay) calls | Proposer calls | Ratchet entries |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| fixed | no tuning | 0 | 0 | 0 | 0 | 0 | 0 | 12 |
| ratchet | promoted; promoted; promoted; no candidate passed the development screen | 3 | 7 | 315 | 356 | 709 | 4 | 44 |
| ratchet_no_memory | promoted; promoted; promoted; no candidate passed the development screen | 3 | 7 | 315 | 356 | 709 | 4 | 44 |
| ratchet_no_replay | promoted; promoted; promoted; no candidate passed the development screen | 3 | 7 | 315 | 629 | 411 | 4 | 44 |
| ratchet_no_compiler | promoted; rejected; rejected; promoted | 2 | 8 | 420 | 719 | 1797 | 4 | 36 |
| prompt_only | rejected; rejected; rejected; rejected | 0 | 8 | 960 | 2070 | 990 | 4 | 12 |
| random | promoted; rejected; rejected; promoted | 2 | 8 | 750 | 1161 | 1621 | 4 | 26 |

Replay (ratchet arm): 556 development task runs replayed exactly from logged calls, 74 needed live execution; live re-check agreement 63/63.

#### Harness × model factorial (final split)

| Cell | Accuracy | Cost | Large-tier calls |
|---|---:|---:|---:|
| baseline/small_only | 45.0% | $0.007661 | 0 |
| baseline/large_only | 66.2% | $0.045447 | 160 |
| baseline/as_configured | 45.0% | $0.007661 | 0 |
| ratchet_final/small_only | 86.9% | $0.004590 | 0 |
| ratchet_final/large_only | 98.1% | $0.027553 | 99 |
| ratchet_final/as_configured | 88.1% | $0.009472 | 22 |

Harness effect with small model: +0.419; with large model: +0.319. Model effect on baseline harness: +0.212; on ratchet harness: +0.112.

#### Interventional attribution of promoted edits (ratchet)

Provenance (the registry) says which edit was promoted, by which proposer, under which review. Attribution says what each edit *caused*. Each edit is removed from the final harness (leave-one-out) or applied alone to the baseline (add-one), and the variants run on the identical sealed final tasks.

Full harness vs baseline: +0.431 (+0.344, +0.525).

| Cycle | Source | Edit | Leave-one-out effect (95% CI) | Add-one effect (95% CI) | Interaction | Review event |
|---:|---|---|---:|---:|---:|---:|
| 1 | known_path_compiler | add_compiled:invoice_reconciliation; add_compiled:refund_request; add_compiled:service_triage | +0.125 (+0.075, +0.175) | +0.150 (+0.094, +0.206) | -0.025 | 9 |
| 2 | harness_ratchet_rules | add_sensor:conflict_requires_abstain; set_tool_retries:1; set_quarantine:True; add_sensor:actions_permitted | +0.175 (+0.113, +0.237) | +0.181 (+0.119, +0.244) | -0.006 | 15 |
| 3 | harness_ratchet_rules | set_context:as_of_filter; set_context:drop_superseded; add_sensor:refs_contain_field; add_escalation:injection_detected | +0.106 (+0.037, +0.175) | +0.138 (+0.050, +0.231) | -0.031 | 21 |

#### Interventional attribution of promoted edits (ratchet_no_compiler)

Provenance (the registry) says which edit was promoted, by which proposer, under which review. Attribution says what each edit *caused*. Each edit is removed from the final harness (leave-one-out) or applied alone to the baseline (add-one), and the variants run on the identical sealed final tasks.

Full harness vs baseline: +0.287 (+0.194, +0.381).

| Cycle | Source | Edit | Leave-one-out effect (95% CI) | Add-one effect (95% CI) | Interaction | Review event |
|---:|---|---|---:|---:|---:|---:|
| 1 | harness_ratchet_rules | set_quarantine:True; add_sensor:actions_permitted; set_context:as_of_filter; add_sensor:conflict_requires_abstain | +0.131 (+0.075, +0.194) | +0.169 (+0.106, +0.231) | -0.038 | 71 |
| 4 | harness_ratchet_rules | set_tool_retries:1; set_context:as_of_filter; set_context:drop_superseded; add_sensor:refs_contain_field | +0.119 (+0.037, +0.206) | +0.156 (+0.069, +0.244) | -0.038 | 87 |

#### Model ledger on the final split (which model did what)

| Arm | Role / model | Calls | Tokens in | Tokens out | Cost |
|---|---|---:|---:|---:|---:|
| fixed | maker/simulated-small-v3 | 160 | 60693 | 3979 | $0.007661 |
| ratchet | maker/simulated-large-v3 | 22 | 7857 | 486 | $0.005881 |
| ratchet | maker/simulated-small-v3 | 77 | 29333 | 1646 | $0.003592 |
| ratchet_no_memory | maker/simulated-large-v3 | 22 | 7857 | 486 | $0.005881 |
| ratchet_no_memory | maker/simulated-small-v3 | 77 | 29333 | 1646 | $0.003592 |
| ratchet_no_replay | maker/simulated-large-v3 | 22 | 7857 | 486 | $0.005881 |
| ratchet_no_replay | maker/simulated-small-v3 | 77 | 29333 | 1646 | $0.003592 |
| ratchet_no_compiler | maker/simulated-small-v3 | 160 | 60128 | 3708 | $0.007496 |
| prompt_only | maker/simulated-small-v3 | 160 | 60693 | 3979 | $0.007661 |
| random | maker/simulated-large-v3 | 144 | 54315 | 3205 | $0.040281 |

#### Adversarial audits

| Honeypot | Rejected | Stage | Detail |
|---|---|---|---|
| forbidden_operation | True | validator | 2 validation errors for HarnessProposal |
| forbidden_tool_grant | True | validator | 2 validation errors for HarnessProposal |
| instruction_smuggling | True | validator | candidate harness is invalid: 1 validation error for HarnessConfig |
| archived_wiki_page | True | validator | 2 validation errors for HarnessProposal |
| draft_wiki_page | True | validator | 2 validation errors for HarnessProposal |
| checker_given_write_authority | True | validator | 2 validation errors for HarnessProposal |
| stale_parent | True | validator | proposal parent does not match the active harness |
| cheap_and_blind | True | promotion_gate | monotone_ratchet, no_family_regression, improvement |
| abstain_heavy | True | promotion_gate | monotone_ratchet, no_family_regression, improvement |

Chaos test (write gate offline): 27 write attempts, 27 failed closed, 0 executed. Passes: True.

Registry hash chain valid: True.

Full reports: [`evidence/v3-simulated/`](evidence/v3-simulated/summary.md) and [`evidence/v3-ollama/`](evidence/v3-ollama/summary.md). Every candidate, its typed diff, its development screen and replay support, the gate's e-values, thresholds, family tests and ratchet results, and every rejection are retained in `report.json`.

## Architecture

```text
             ┌──────────── trusted, immutable ────────────┐
  tasks ──▶  │ executor (tool grants, budgets, write gate) │ ◀── harness (mutable, typed)
             └─────────────────────────────────────────────┘
                 │ retrieve → compiled? → context → route → model → sensors → retry/abstain → gate
                 ▼
  traces + content-addressed model calls ──▶ independent verifier (labels never reachable)
                 │
   diagnostics (failure clusters) ──▶ proposer (rules | Ollama) + known-path compiler + memory
                 │
   validator (language, parent hash, allowed ops) ──▶ dev screen (support-aware replay)
                 │
   sequential paired selection: e-process at α_t, Holm family tests, monotone ratchet, invariants
                 │
   review bound to candidate + report hashes ──▶ bi-temporal activation ──▶ ratchet grows
                 │
              next cycle (the promoted harness is the new parent)        … sealed final, once
```

## Quick start

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest; ruff check .

# Offline (CPU only, about 1 minute): all arms, audits, sealed final, factorial
rasis-v3 run --provider simulated --approval standing --db data\v3-sim.sqlite3 --out results\v3-sim

# Local models (no network, $0): requires Ollama with llama3.2:3b and qwen2.5:7b pulled.
# Resumable: re-run the identical command until it exits 0 (exit 3 = time box spent).
rasis-v3 run --provider ollama --proposer ollama --arms fixed,ratchet,ratchet_no_compiler,prompt_only,random `
  --n-dev 12 --n-selection 25 --n-final 20 --cycles 3 --approval standing `
  --db data\v3-ollama.sqlite3 --out results\v3-ollama `
  --cache data\v3-ollama-cache.sqlite3 --max-seconds 360

# Robustness: offline seed x assumption sweep (~6 min); real-model replication on fresh tasks
rasis-v3 sweep --seeds 0,1,2,3,4 --guide-effects 1.0,0.6,0.2,0.0 --out results\v3-sweep
rasis-v3 confirm --report evidence\v3-ollama\report.json --arms fixed,ratchet --n-final 40 `
  --task-seed-offset 100 --out results\v3-confirm --cache data\v3-confirm-cache.sqlite3 --max-seconds 360

# Inspect lineage
rasis-v3 history --db data\v3-sim.sqlite3 --arm ratchet
rasis-v3 pack --db data\v3-sim.sqlite3 --arm ratchet --review-seq <seq>
rasis-v3 prov --db data\v3-sim.sqlite3 --out results\prov.json
rasis-v3 rollback --db data\v3-sim.sqlite3 --arm ratchet --to <accepted-hash> --reviewer <name> --reason "..."
```

`--approval pending` (the default) stops an arm at the first gate-eligible candidate and records a pending review without activating anything. `--approval standing` records that the named reviewer pre-authorised gate-eligible promotions for that run. This is a standing approval, recorded on every decision, **not** a per-candidate human review. The reviewer name is self-reported local metadata, not authentication.

## Arms and budget

All tuned arms get the same number of cycles, proposals per cycle and selection tests. Fixed baselines get no tuning. Model-call totals are reported, not equalised.

| Arm | Proposer | Compiler | Replay | Memory |
|---|---|---|---|---|
| `fixed` | none | – | – | – |
| `ratchet` | structural (rules offline, Ollama for the real run) | ✓ | ✓ | ✓ |
| `ratchet_no_memory` / `ratchet_no_replay` / `ratchet_no_compiler` | as `ratchet`, minus one component | | | |
| `prompt_only` | same proposer family, restricted to guide and free-text operations (a reflection-style baseline) | – | – | ✓ |
| `random` | uniformly random valid edits from the full language | – | – | – |

## Limitations

- **Synthetic tasks, coupled generator.** The labels, the known-path compiler and the simulated model all follow the documented resolution rule, so compiled accuracy on covered tasks holds by construction. What is measured is coverage, skip behaviour and cost. The held-out family and final-only drift are genuine distribution shifts *within* this generator, not evidence of real-world transfer.
- **Attribution is causal only for this task distribution and harness lineage.** Leave-one-out and add-one effects are interventions on fixed tasks. Removing one edit's operations can interact with later edits to the same field, and such cases are marked non-separable.
- **The simulated backend encodes its assumptions** (see above), including the simulated checker's detection and false-alarm rates. The sensitivity sweep shows the main offline conclusion does not depend on the guide-strength assumption, but the other error rates were not swept. Only the Ollama results speak to model behaviour. They come from two small local models: one improvement run (one seed) plus one replication of the frozen harnesses on fresh tasks. The improvement loop itself was not repeated across seeds with real models.
- **Selection data is reused across cycles.** Error spending controls the false-promotion rate for the *selection* tests. It does not remove adaptivity from proposals built on development diagnostics. The final split is evaluated once, after every arm is frozen, but its generator and seed are public: "sealed" is procedural, not cryptographic.
- **Heuristic content boundary.** The free-text guidance filter is a regex for this study, not a general prompt-injection defence.
- **Cost is a proxy.** USD figures apply illustrative hosted-equivalent prices (`PRICE_ASSUMPTIONS`) to measured token counts. Local inference had no monetary cost; Ollama wall-clock latency is recorded per call.
- **Not production governance.** The write gate, escrow, ceiling, registry triggers and hash chains are local mechanisms tested by this repository's tests. They are not production authentication, isolation or tamper-proof audit storage. Rollback re-activates a previous configuration and cannot undo external actions.
- The improvement mechanism is bounded harness configuration search. It is not weight training, unrestricted self-modifying code or open-ended self-improvement, and the optimiser does not improve its own algorithm.

## Earlier studies (preserved unchanged)

v1 (single proposal, three retrieval settings) and v2 (multi-cycle, multi-tool) are kept exactly as published, including v2's null result. Their commands use the `rasis` entry point.

A small, reproducible experiment in improving an inference-time agent runtime through bounded strategy changes, independently checked outcomes, and an auditable local review loop.

The research question is whether trace-informed changes to evidence retrieval improve verified outcomes or cost on held-out task instances while fixed controls remain intact. This is not weight training, unrestricted self-modifying code, a business-workflow discovery engine, or a claim of open-ended improvement.

### Multi-cycle study (v2)

The newer study adds family-specific read tools, fact-based synthetic tasks, source-support checks, a strong fixed baseline, an independent one-draw non-trace search, and up to three review-gated selection cycles. It is separate from the original v1 demo below; use a **new SQLite database** for each study.

```powershell
python -m pip install -e ".[dev]"
rasis --db data\study-v2.sqlite3 --study cycle --provider fake --output results\cycle-1.json
$env:RASIS_DB_PATH = "data\study-v2.sqlite3"
uvicorn recursive_self_improvement_agent.api:app
```

Open `http://127.0.0.1:8000/study/review/1` to inspect the strategy diff, trace-linked diagnostics, paired regressions, blocked requests, provider usage, and decision history. Accepting requires an eligible proposal and binds the exact report and version hashes; a rejected proposal leaves the active strategy unchanged. The reviewer field is self-reported local metadata, not authentication.

Start another cycle only after review. As a convenience, `--auto-reject-ineligible` records an explicitly **system** rejection for an ineligible proposal; it never approves a proposal or represents that a human reviewed it. At most three proposal attempts are allowed, with a study-wide ceiling of 1,500 synthetic tool calls. The executor checks the remaining budget before every call; interrupted attempts retain a conservative reservation rather than silently disappearing.

```powershell
rasis --db data\study-v2.sqlite3 --study cycle --provider fake --auto-reject-ineligible
rasis --db data\study-v2.sqlite3 --study history --output results\study-history.json
```

To restore an accepted configuration, submit its hash, local reviewer and reason to `POST /study/rollback`; rollback changes only the active configuration pointer. An accepted, currently active strategy may be frozen for **one** final comparison:

```powershell
rasis --db data\study-v2.sqlite3 --study final --cycle <cycle-number> `
  --report-hash "<selection-report-hash>" --candidate-hash "<version-hash>" `
  --output results\study-final.json
```

The final split is loaded only after that review. It contains temporal-boundary and excess-unverified-source variants not used in development or selection. The generator and seeds are public, so “sealed” here is an operational convention, not a secrecy or isolation guarantee.

#### Study design and evidence

- Generator `multi-tool-v2` uses seeds 4103–4105 for the three development cycles, 4207 for selection, 4301 for final, and 7001–7003 for the deterministic one-draw non-trace search. Each split has nine tasks per family.
- Evidence reconciliation reads dated invoice records; customer cases combine account and policy facts; operational triage combines telemetry and thresholds. The runtime sees raw facts, not expected decision labels. The verifier checks the decision **and** the exact authoritative source references actually retrieved; missing inputs require abstention.
- Each cycle compares the proposal with the active configuration, a strong fixed `current_first` strategy with three evidence items, a two-call verification heuristic, and one randomly chosen bounded strategy. Trace-informed and non-trace methods each get one development run and one proposal attempt; fixed baselines use no tuning. This is a deliberately small equal-attempt comparison, **not** an exhaustive search or equal model-token spend.
- The fixed rule requires no per-family or aggregate accuracy regression against any comparator, no executed prohibited actions in the adversarial checks, and strict accuracy or tool-call improvement over both active and strong fixed strategies. The rule was fixed before each recorded cycle but designed after earlier exploratory work; this is **not** a preregistered confirmatory study.
- Reports retain paired task traces, evidence references, proposed changes, development diagnostics, actual tool calls, abstentions, failures, blocked attempts, executed-violation counters, per-family results, and paired wins/losses/ties with descriptive standard errors. Model usage and user-supplied price assumptions are recorded separately. Total history reports unknown cost as `null` if pricing or interrupted provider usage is unknown.
- The fake provider is a deterministic rule, not a model; it does not demonstrate autonomous reasoning. The real adapter is available with the opt-in environment variables described below: use `--study cycle --provider openai-compatible`. It sends synthetic development diagnostics only. The adapter is tested with a mocked response; **no real model run or real-model performance result is claimed**.

The published exploratory run is [evidence/study-v2-offline.json](evidence/study-v2-offline.json). All three proposals were system-rejected: each failed to beat the strong fixed strategy. In selection, proposed/strong-fixed correct counts were 27/27 in every cycle; the proposal used 27, 27 and 54 calls respectively, versus 27 each for strong fixed. The heuristic also had 27/27 and used 54 calls; the one-draw search had 6/27, 27/27 and 27/27. Three deliberately prohibited requests per cycle were blocked; no violations executed. The three cycles used 687 tool calls, three fake proposal calls, zero model tokens and $0 synthetic/provider cost. No human approval or final evaluation was performed.

This negative result is the main finding: on these constructed tasks, the trace-informed strategy did **not** outperform a strong fixed strategy. The tasks remain synthetic, family outcomes share generator assumptions, and the sample is too small to establish production performance or cross-domain transfer. Full reports and decisions are local SQLite records with hash checks, not a tamper-proof audit system or production sandbox.

### Status and scope

This repository implements a CPU-only, offline vertical slice:

- Three synthetic task families share one task, evidence-tool, runtime, and verifier interface: evidence reconciliation, customer cases, and operational triage.
- A fixed strategy, a simple hand-authored heuristic, and a trace-informed bounded proposal are compared.
- The only mutable strategy fields are an enumerated retrieval strategy (`single_pass`, `cross_check`, or `current_first`), a 1–3 evidence limit, and a verification toggle.
- A trusted executor restricts tools to scoped evidence search. Runtime configuration cannot change tools, task scope, labels, evaluator, deadlines, budgets, or promotion policy.
- LangGraph runs a finite retrieve → verify → answer graph. SQLite stores immutable strategy versions, report hashes, review decisions, and the active-version pointer.
- A fake proposal provider is deterministic and offline. A real OpenAI-compatible provider is separate and opt-in; it receives only synthetic development diagnostics.
- Human review is bound to the exact immutable version and report hash. Rollback changes the local configuration pointer only; it cannot reverse external actions.

The reviewer name and SQLite registry are illustrative local workflow mechanisms, not production authentication, multi-user access control, or isolation. The final split is inaccessible through runtime tools and is only loaded by the evaluation CLI after the strategy and selection review are frozen. Source code and generator are public, so this is a procedural sealed split, not cryptographic secrecy.

There is no UI, deployment integration, real customer data, model-generated executable code, external write action, or statistical claim of cross-family transfer. A real model is used only for the optional strategy-proposal step, not for task answers. The offline fake proposal is intentionally scripted. These are limitations, not implemented features.

### Quick start (Windows, Python 3.11+)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
rasis --provider fake --db data\offline.sqlite3 --output results\offline-run.json
```

The command runs without network access or a GPU. It writes the actual run summary, paired per-family outcomes, model usage, costs, and immutable local records. The SQLite file and generated run report are local outputs; do not treat them as production audit storage.

The demo deliberately stops with the review pending; it does not manufacture a human approval or run the sealed set automatically. To review, start the API against the same registry:

```powershell
$env:RASIS_DB_PATH = "data\offline.sqlite3"
uvicorn recursive_self_improvement_agent.api:app --reload
```

Inspect the version configuration and selection report at `/versions` and `/reports/{report_id}`. Then submit an explicit local review decision (replace the IDs with those printed in `results\offline-run.json`):

```powershell
$body = @{
  candidate_version_id = "<candidate-version-id>"
  report_id = "<selection-report-id>"
  reviewer = "local-reviewer"
  accepted = $true
  reason = "Reviewed the exact strategy diff and paired selection evidence."
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8000/reviews" -Method Post `
  -ContentType "application/json" -Body $body
```

Only after that accepted review, run the final comparison once:

```powershell
rasis --db data\offline.sqlite3 --finalize-version "<candidate-version-id>" `
  --selection-report "<selection-report-id>" --output results\final-run.json
```

The registry rejects a second final evaluation for that selection report. The API reviewer field is self-reported local metadata, not authenticated identity.

To run tests and focused quality checks:

```powershell
pytest
ruff check .
ruff format --check .
```

Start the local illustrative API after installing the package:

```powershell
$env:RASIS_DB_PATH = "data\offline.sqlite3"
uvicorn recursive_self_improvement_agent.api:app --reload
```

The API exposes health, version/diff listing, report inspection, decision history, bounded version creation, review, and rollback. It does not expose final-test labels or an evaluation endpoint.

### Optional real proposal model

No provider request is made unless `--provider openai-compatible` is selected. Use an OpenAI-compatible chat-completions endpoint supporting strict JSON schema output:

```powershell
$env:OPENAI_API_KEY = "<your-key>"
$env:OPENAI_MODEL = "<model-id>"
$env:OPENAI_BASE_URL = "https://api.openai.com/v1/chat/completions"
$env:MODEL_INPUT_USD_PER_MILLION = "<provider input price>"
$env:MODEL_OUTPUT_USD_PER_MILLION = "<provider output price>"
rasis --provider openai-compatible --db data\online-opt-in.sqlite3 --output results\online-opt-in.json
```

Only compact synthetic development failure summaries and evidence-reference IDs are submitted. No task text, private documents, credentials other than the authorization header, held-out labels, or evaluator internals are sent. The model identifier, reported token usage, pricing assumptions, and computed proposal cost are recorded. If prices are omitted, usage is recorded but proposal USD cost is `null`; check current provider pricing yourself. Network failures and invalid model output fail explicitly rather than silently falling back to the fake.

### What the experiment does

1. Generate six tasks per family for each split from versioned generator `synthetic-v1`, using fixed seeds `1103` (development), `2207` (selection), and `3301` (final).
2. Run the fixed runtime on development cases and build evidence-linked diagnostics from misses. The deterministic fake uses trace references to choose between stale-record filtering and cross-check retrieval; it is a reproducible stand-in, not model reasoning. The opt-in model proposes only retrieval strategy, evidence limit (1–3), and verification policy.
3. Validate the strict Pydantic config; immutable trusted controls are not part of the proposal schema or the agent's tool surface.
4. On selection cases, compare the proposed strategy and simple heuristic against the fixed strategy using the same paired tasks. The promotion rule is defined in `evaluation.PROMOTION_RULE`: no executed prohibited actions, no aggregate or per-family regression against either baseline, and a strict accuracy or tool-call improvement against both. A selection-time rule failure cannot be accepted by the registry.
5. A reviewer inspects the exact strategy version and selection report, then posts an explicit decision. The decision binds both hashes. Only after that freeze/review is the final split loaded once for the fixed-versus-proposed comparison.

Expected labels are retained by the evaluator, not in `AgentTask` and not returned by `search_evidence`. Labels are generated from the synthetic case recipe. The v1 verifier checks the output decision, retrieved references, and their current-source stance against the synthetic fixture. The synthetic distractor is deliberately misleading and appears before the current source for the fixed one-item retrieval strategy. Missing-information cases include both empty and partial evidence.

The suite includes paired win/loss/tie counts and the standard error of paired accuracy differences. With only 18 synthetic cases per split, these are descriptive, not evidence of population-level statistical significance. Reported USD tool cost is zero because local synthetic tools have no provider charge; actual tool calls are also reported and are the offline resource proxy. Proposal model usage/cost is accounted separately. The output summary accounts for development, heuristic/fixed selection, proposed/fixed selection, final evaluation, and proposal usage so tuning is not hidden behind final inference. Full paired task inputs, labels, and structured traces are stored in the hashed report; they are evaluator output, not tools granted to the runtime.

### Controls and failure semantics

- `RuntimeConfig` is strict and immutable. Extra fields such as permissions, labels, task scope, deadlines, or promotion policy are rejected.
- `TrustedToolExecutor` enforces a one-task scope, an allowlist, argument bounds, and a four-call per-run ceiling independent of strategy or prompt.
- The executor also applies a fixed five-second deadline and the experiment registry enforces a 200-tool-call total ceiling across stored evaluation traces.
- Prohibited tool requests are recorded as **blocked attempts**. The executor does not expose an evaluator-label tool; an executed prohibited-action counter is reported separately.
- Tool budget exhaustion stops the graph and yields an explicit abstention/failure trace.
- Invalid, unobserved, or unsupported references fail the fixture-specific checks; these are synthetic assertions, not a general natural-language entailment verifier.
- Human decisions are append-only and bind exact version/report hashes. Registry corruption invalidates review. Local identity is self-reported and is not authentication.

These tested controls are finite application checks, not a proof of security or production isolation.

### Architecture

```text
Synthetic task generator ──> Fixed / proposed runtime strategies
                                     │
                          LangGraph retrieve → verify → answer
                                     │
                       Trusted scoped evidence-tool executor
                                     │
                  Structured traces ─┴─ Independent verifier
                         │                     │
                   Diagnostics          Paired evaluation
                         │                     │
             Constrained proposal ───> SQLite version/review registry
                                               │
                                       frozen final run
```

Key modules: `models.py` (typed contracts), `data.py` (versioned synthetic generator), `runtime.py` (bounded graph and executor), `evaluation.py` (independent verifier and promotion rule), `improvement.py` (fake and opt-in provider), `store.py` (append-only records), `api.py` (local review/version API), and `cli.py` (reproducible experiment).

### Data, assumptions, and evidence

- Every split contains all three families; seeded case identifiers differ by split. Final cases add a second conflicting stale source and additional partial records, making them held-out edge variants, **not** a held-out family or evidence of cross-family transfer.
- Each family has six balanced tasks: two supported, two contradicted, and two insufficient. The labels derive from the generator's fixed recipe. This makes the benchmark intentionally transparent and risks evaluator/generator coupling.
- Document evidence and decisions are synthetic. Current and stale records are created deterministically; the random shuffling seed is disclosed, although retrieval ranking is deterministic by reference.
- All consequential actions are simulated. No benchmark was sourced from private or customer material.
- A run report is an experiment artifact, not proof of real-world reliability. Do not compare this synthetic accuracy to deployed systems or other benchmarks.
- The only model usage this slice supports is optional proposal generation. Record model ID and actual input/output token usage from the response. Pricing is an explicit user-supplied assumption; no model has been called or priced by default.
- Initial baseline and every proposed version are immutable. Re-running into the same SQLite path retains history and may reject duplicate report IDs only by database constraint; use a fresh path for a clean reproduction.

### Verified offline run

Observed 14 November 2026 with generator `synthetic-v1`, the documented fixed split seeds, and the deterministic fake proposal provider. The v1 strategy and selection rule were adjusted after inspecting intermediate results, so this is exploratory, not preregistered or held-out final evidence:

| Evaluation | Strategy | Accuracy | Tool calls | Abstentions | Failures | USD tool cost |
|---|---|---:|---:|---:|---:|---:|
| Development (18 tasks) | Fixed | 6/18 (33.3%) | 18 | 6 | 0 | $0.00 |
| Selection (18 paired tasks) | Fixed | 6/18 (33.3%) | 18 | 6 | 0 | $0.00 |
| Selection (18 paired tasks) | Simple heuristic | 18/18 (100%) | 36 | 6 | 0 | $0.00 |
| Selection (18 paired tasks) | Trace-informed `current_first` | 18/18 (100%) | 18 | 6 | 0 | $0.00 |

The proposed strategy vs fixed had 12 paired wins, 0 losses, 6 ties; paired accuracy difference was 0.6667 with standard error 0.1143. Each selection family was 6/6 for the heuristic and proposed strategy and 2/6 for fixed. There were no blocked prohibited attempts or executed prohibited violations in these runs. The proposal beat the heuristic on tool calls while matching its outcomes, so it met the predeclared selection eligibility rule. This small, generator-coupled result does not establish a general cost or reliability benefit.

The run used 126 of the fixed 200 tool-call budget across development, both selection comparisons, and the proposal step. Local synthetic tool USD cost was $0.00; fake-provider token use and cost were both zero. Human review remains pending, so the selected version was not promoted and the sealed final evaluation has **not** run. Preserve this pending/review-gated state rather than treating selection as final-test evidence.

- Hardware model and wall-clock measurements were not captured; no latency, energy, or throughput claim is made.

### Trade-offs and current gaps

- SQLite and a self-reported reviewer are suitable only for a single-user local demonstration. There is no production auth, tenancy, isolation, or tamper-resistant audit log.
- A one-shot deterministic fake makes offline behavior repeatable but does not demonstrate autonomous model reasoning. The optional model integration applies to proposal generation only.
- Synthetic families share a simple evidence format; this first slice does not establish transfer to different tool schemas or real task distributions.
- The compact experiment has no multi-seed confidence study, large-scale selection budget search, subjective model judge, dashboard, or external deployment.
- The sealed final split is operationally withheld until the CLI's post-review stage, but its generator and seed are visible in this open-source repository.
- Rollback selects a previous accepted local configuration. It does not undo actions performed elsewhere.

Negative or null outcomes must be retained in the generated report; never change the promotion rule after observing final results.
