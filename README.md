# Recursive Agent Self-Improvement

A small, reproducible experiment in improving an inference-time agent runtime through bounded strategy changes, independently checked outcomes, and an auditable local review loop.

The research question is whether trace-informed changes to evidence retrieval improve verified outcomes or cost on held-out task instances while fixed controls remain intact. This is not weight training, unrestricted self-modifying code, a business-workflow discovery engine, or a claim of open-ended improvement.

## Multi-cycle study (v2)

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

### Study design and evidence

- Generator `multi-tool-v2` uses seeds 4103–4105 for the three development cycles, 4207 for selection, 4301 for final, and 7001–7003 for the deterministic one-draw non-trace search. Each split has nine tasks per family.
- Evidence reconciliation reads dated invoice records; customer cases combine account and policy facts; operational triage combines telemetry and thresholds. The runtime sees raw facts, not expected decision labels. The verifier checks the decision **and** the exact authoritative source references actually retrieved; missing inputs require abstention.
- Each cycle compares the proposal with the active configuration, a strong fixed `current_first` strategy with three evidence items, a two-call verification heuristic, and one randomly chosen bounded strategy. Trace-informed and non-trace methods each get one development run and one proposal attempt; fixed baselines use no tuning. This is a deliberately small equal-attempt comparison, **not** an exhaustive search or equal model-token spend.
- The fixed rule requires no per-family or aggregate accuracy regression against any comparator, no executed prohibited actions in the adversarial checks, and strict accuracy or tool-call improvement over both active and strong fixed strategies. The rule was fixed before each recorded cycle but designed after earlier exploratory work; this is **not** a preregistered confirmatory study.
- Reports retain paired task traces, evidence references, proposed changes, development diagnostics, actual tool calls, abstentions, failures, blocked attempts, executed-violation counters, per-family results, and paired wins/losses/ties with descriptive standard errors. Model usage and user-supplied price assumptions are recorded separately. Total history reports unknown cost as `null` if pricing or interrupted provider usage is unknown.
- The fake provider is a deterministic rule, not a model; it does not demonstrate autonomous reasoning. The real adapter is available with the opt-in environment variables described below: use `--study cycle --provider openai-compatible`. It sends synthetic development diagnostics only. The adapter is tested with a mocked response; **no real model run or real-model performance result is claimed**.

The published exploratory run is [evidence/study-v2-offline.json](evidence/study-v2-offline.json). All three proposals were system-rejected: each failed to beat the strong fixed strategy. In selection, proposed/strong-fixed correct counts were 27/27 in every cycle; the proposal used 27, 27 and 54 calls respectively, versus 27 each for strong fixed. The heuristic also had 27/27 and used 54 calls; the one-draw search had 6/27, 27/27 and 27/27. Three deliberately prohibited requests per cycle were blocked; no violations executed. The three cycles used 687 tool calls, three fake proposal calls, zero model tokens and $0 synthetic/provider cost. No human approval or final evaluation was performed.

This negative result is the main finding: on these constructed tasks, the trace-informed strategy did **not** outperform a strong fixed strategy. The tasks remain synthetic, family outcomes share generator assumptions, and the sample is too small to establish production performance or cross-domain transfer. Full reports and decisions are local SQLite records with hash checks, not a tamper-proof audit system or production sandbox.

## Status and scope

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

## Quick start (Windows, Python 3.11+)

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

## Optional real proposal model

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

## What the experiment does

1. Generate six tasks per family for each split from versioned generator `synthetic-v1`, using fixed seeds `1103` (development), `2207` (selection), and `3301` (final).
2. Run the fixed runtime on development cases and build evidence-linked diagnostics from misses. The deterministic fake uses trace references to choose between stale-record filtering and cross-check retrieval; it is a reproducible stand-in, not model reasoning. The opt-in model proposes only retrieval strategy, evidence limit (1–3), and verification policy.
3. Validate the strict Pydantic config; immutable trusted controls are not part of the proposal schema or the agent's tool surface.
4. On selection cases, compare the proposed strategy and simple heuristic against the fixed strategy using the same paired tasks. The promotion rule is defined in `evaluation.PROMOTION_RULE`: no executed prohibited actions, no aggregate or per-family regression against either baseline, and a strict accuracy or tool-call improvement against both. A selection-time rule failure cannot be accepted by the registry.
5. A reviewer inspects the exact strategy version and selection report, then posts an explicit decision. The decision binds both hashes. Only after that freeze/review is the final split loaded once for the fixed-versus-proposed comparison.

Expected labels are retained by the evaluator, not in `AgentTask` and not returned by `search_evidence`. Labels are generated from the synthetic case recipe. The v1 verifier checks the output decision, retrieved references, and their current-source stance against the synthetic fixture. The synthetic distractor is deliberately misleading and appears before the current source for the fixed one-item retrieval strategy. Missing-information cases include both empty and partial evidence.

The suite includes paired win/loss/tie counts and the standard error of paired accuracy differences. With only 18 synthetic cases per split, these are descriptive, not evidence of population-level statistical significance. Reported USD tool cost is zero because local synthetic tools have no provider charge; actual tool calls are also reported and are the offline resource proxy. Proposal model usage/cost is accounted separately. The output summary accounts for development, heuristic/fixed selection, proposed/fixed selection, final evaluation, and proposal usage so tuning is not hidden behind final inference. Full paired task inputs, labels, and structured traces are stored in the hashed report; they are evaluator output, not tools granted to the runtime.

## Controls and failure semantics

- `RuntimeConfig` is strict and immutable. Extra fields such as permissions, labels, task scope, deadlines, or promotion policy are rejected.
- `TrustedToolExecutor` enforces a one-task scope, an allowlist, argument bounds, and a four-call per-run ceiling independent of strategy or prompt.
- The executor also applies a fixed five-second deadline and the experiment registry enforces a 200-tool-call total ceiling across stored evaluation traces.
- Prohibited tool requests are recorded as **blocked attempts**. The executor does not expose an evaluator-label tool; an executed prohibited-action counter is reported separately.
- Tool budget exhaustion stops the graph and yields an explicit abstention/failure trace.
- Invalid, unobserved, or unsupported references fail the fixture-specific checks; these are synthetic assertions, not a general natural-language entailment verifier.
- Human decisions are append-only and bind exact version/report hashes. Registry corruption invalidates review. Local identity is self-reported and is not authentication.

These tested controls are finite application checks, not a proof of security or production isolation.

## Architecture

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

## Data, assumptions, and evidence

- Every split contains all three families; seeded case identifiers differ by split. Final cases add a second conflicting stale source and additional partial records, making them held-out edge variants, **not** a held-out family or evidence of cross-family transfer.
- Each family has six balanced tasks: two supported, two contradicted, and two insufficient. The labels derive from the generator's fixed recipe. This makes the benchmark intentionally transparent and risks evaluator/generator coupling.
- Document evidence and decisions are synthetic. Current and stale records are created deterministically; the random shuffling seed is disclosed, although retrieval ranking is deterministic by reference.
- All consequential actions are simulated. No benchmark was sourced from private or customer material.
- A run report is an experiment artifact, not proof of real-world reliability. Do not compare this synthetic accuracy to deployed systems or other benchmarks.
- The only model usage this slice supports is optional proposal generation. Record model ID and actual input/output token usage from the response. Pricing is an explicit user-supplied assumption; no model has been called or priced by default.
- Initial baseline and every proposed version are immutable. Re-running into the same SQLite path retains history and may reject duplicate report IDs only by database constraint; use a fresh path for a clean reproduction.

## Verified offline run

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

## Trade-offs and current gaps

- SQLite and a self-reported reviewer are suitable only for a single-user local demonstration. There is no production auth, tenancy, isolation, or tamper-resistant audit log.
- A one-shot deterministic fake makes offline behavior repeatable but does not demonstrate autonomous model reasoning. The optional model integration applies to proposal generation only.
- Synthetic families share a simple evidence format; this first slice does not establish transfer to different tool schemas or real task distributions.
- The compact experiment has no multi-seed confidence study, large-scale selection budget search, subjective model judge, dashboard, or external deployment.
- The sealed final split is operationally withheld until the CLI's post-review stage, but its generator and seed are visible in this open-source repository.
- Rollback selects a previous accepted local configuration. It does not undo actions performed elsewhere.

Negative or null outcomes must be retained in the generated report; never change the promotion rule after observing final results.

## Roadmap (v3, in progress)

v1 and v2 are retained unchanged as history, including the v2 null result. The planned v3 moves the mutable surface from three retrieval settings to the agent **harness** itself, as a typed, non-executable configuration language:

- advisory guides, declarative output sensors, and the promotion of a guide into an enforcing check;
- per-step routing between a small and a larger local model (Ollama), with a cascade when a sensor fails;
- a monotonically growing regression suite in which every accepted failure class becomes a permanent negative test;
- anytime-valid paired promotion statistics with error spending across repeated cycles;
- support-aware replay screening before live execution;
- a harder, unsaturated benchmark with a fully held-out task family.

None of this is implemented until it appears in the sections above with evidence.
