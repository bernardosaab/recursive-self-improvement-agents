# Confirmation on fresh final tasks

Frozen harnesses from `evidence/v3-ollama/report.json` (hashes verified), evaluated once on 80 new final-split tasks (seed 751886, 20 per family including the held-out family). Backend `ollama` (small `ollama/llama3.2:3b`, large `ollama/qwen2.5:7b`).

| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) | McNemar p | Held-out Δ (p) |
|---|---:|---:|---|---:|---:|---:|
| fixed | 5.0% (2.0–12.2) | 0.0% | – | – | – | – |
| ratchet | 62.5% (51.5–72.3) | 40.0% | 46/0/34 | +0.575 (+0.463, +0.675) | 2.84e-14 | +0.400 (0.00781) |
| ratchet@checker_on_hazard | 65.0% (54.1–74.5) | 25.0% | 49/1/30 | +0.600 (+0.487, +0.700) | 9.06e-14 | +0.250 (0.0625) |

## Variants vs the frozen ratchet harness

| Variant | vs ratchet W/L/T | Δ (95% CI) | McNemar p | Held-out Δ (p) |
|---|---|---:|---:|---:|
| ratchet@checker_on_hazard | 6/4/70 | +0.025 (-0.050, +0.100) | 0.754 | -0.150 (0.25) |

## Where the accuracy comes from, and what it cost

Compiled tasks are answered by a zero-model recipe; model-answered tasks went through the model (accuracy (n)). Wall time is measured end to end on this machine; GPU energy is mean sampled board power × wall time (nvidia-smi, best effort).

| Harness | Compiled | Model-answered | Model calls | Large-tier calls | Wall s | s / verified completion | Wh / verified completion | Proxy USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | – (0) | 5.0% (80) | 80 | 0 | 83 | 20.8 | 0.481 | $0.0059 |
| ratchet | 100.0% (27) | 43.4% (53) | 53 | 53 | 71 | 1.9 | 0.035 | $0.0210 |
| ratchet@checker_on_hazard | 100.0% (27) | 47.2% (53) | 78 | 78 | 100 | 2.9 | 0.064 | $0.0334 |

`ratchet@small_only` forces default routing to the small model; `ratchet@checker_*` adds an independent large-tier checker role (no tools) to the frozen ratchet harness.
