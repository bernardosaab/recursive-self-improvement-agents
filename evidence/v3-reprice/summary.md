# Price sensitivity of cost per verified completion

Measured final-split token counts from `evidence/v3-ollama/report.json`, re-priced under illustrative two-tier sheets (USD per million input/output tokens; generic tiers, not provider quotes): `published_default` small 0.1/0.4, large 0.6/2.4; `both_cheap` small 0.05/0.2, large 0.2/0.8; `narrow_gap` small 0.3/1.2, large 0.6/2.4; `wide_gap` small 0.1/0.4, large 3.0/15.0.

| Arm | Verified completions | `published_default` | `both_cheap` | `narrow_gap` | `wide_gap` |
|---|---:|---:|---:|---:|---:|
| fixed | 6 | $0.00094 | $0.00047 | $0.00283 | $0.00094 |
| prompt_only | 6 | $0.00094 | $0.00047 | $0.00283 | $0.00094 |
| random | 27 | $0.00102 | $0.00034 | $0.00102 | $0.00542 |
| ratchet | 37 | $0.00055 | $0.00018 | $0.00055 | $0.00295 |
| ratchet_no_compiler | 27 | $0.00115 | $0.00038 | $0.00115 | $0.00612 |
| **ratchet ÷ fixed** | | **0.59×** | **0.39×** | **0.20×** | **3.13×** |
