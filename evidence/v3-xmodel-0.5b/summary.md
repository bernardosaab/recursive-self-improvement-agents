# Confirmation on fresh final tasks

Frozen harnesses from `evidence/v3-ollama/report.json` (hashes verified), evaluated once on 80 new final-split tasks (seed 751886, 20 per family including the held-out family). Backend `ollama` (small `ollama/qwen2.5:0.5b`, large `ollama/qwen2.5:7b`).

| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) | McNemar p | Held-out Δ (p) |
|---|---:|---:|---|---:|---:|---:|
| fixed | 0.0% (0.0–4.6) | 0.0% | – | – | – | – |
| ratchet | 62.5% (51.5–72.3) | 40.0% | 50/0/30 | +0.625 (+0.512, +0.725) | 1.78e-15 | +0.400 (0.00781) |
| ratchet@small_only | 48.8% (38.1–59.5) | 15.0% | 39/0/41 | +0.487 (+0.375, +0.600) | 3.64e-12 | +0.150 (0.25) |

## Variants vs the frozen ratchet harness

| Variant | vs ratchet W/L/T | Δ (95% CI) | McNemar p | Held-out Δ (p) |
|---|---|---:|---:|---:|
| ratchet@small_only | 0/11/69 | -0.138 (-0.212, -0.062) | 0.000977 | -0.250 (0.0625) |

## Where the accuracy comes from, and what it cost

Compiled tasks are answered by a zero-model recipe; model-answered tasks went through the model (accuracy (n)). Wall time is measured end to end on this machine; GPU energy is mean sampled board power × wall time (nvidia-smi, best effort).

| Harness | Compiled | Model-answered | Model calls | Large-tier calls | Wall s | s / verified completion | Wh / verified completion | Proxy USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | – (0) | 0.0% (80) | 80 | 0 | 43 | – | – | $0.0052 |
| ratchet | 100.0% (27) | 43.4% (53) | 53 | 53 | 51 | 1.3 | 0.029 | $0.0210 |
| ratchet@small_only | 100.0% (27) | 22.6% (53) | 65 | 12 | 28 | 1.0 | 0.022 | $0.0088 |

`ratchet@small_only` forces default routing to the small model; `ratchet@checker_*` adds an independent large-tier checker role (no tools) to the frozen ratchet harness.
