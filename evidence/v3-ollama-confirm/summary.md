# Confirmation on fresh final tasks

Frozen harnesses from `evidence\v3-ollama\report.json` (hashes verified), evaluated once on 160 new final-split tasks (seed 5407, 40 per family including the held-out family). Backend `ollama` (ollama/llama3.2:3b, ollama/qwen2.5:7b).

| Harness | Accuracy (95% CI) | Held-out family | vs fixed W/L/T | Δ vs fixed (95% CI) | McNemar p | Held-out Δ (p) | Cost |
|---|---:|---:|---|---:|---:|---:|---:|
| fixed | 2.5% (1.0–6.3) | 0.0% | – | – | – | – | $0.0121 |
| ratchet | 57.5% (49.8–64.9) | 27.5% | 88/0/72 | +0.550 (+0.475, +0.625) | 6.46e-27 | +0.275 (0.000977) | $0.0429 |
| ratchet@small_only | 50.6% (43.0–58.3) | 12.5% | 78/1/81 | +0.481 (+0.400, +0.556) | 2.65e-22 | +0.125 (0.0625) | $0.0197 |

`ratchet@small_only` is the final ratchet harness with default routing forced to the small model, isolating the harness effect from the proposer's choice to route to the large model.
