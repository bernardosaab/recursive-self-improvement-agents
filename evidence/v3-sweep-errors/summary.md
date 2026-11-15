# Offline sensitivity sweep

Seeds [0, 1, 2, 3, 4] (each seed shifts every split seed by 1000×seed and reseeds the simulator). Guide effect = multiplier on a failure mode's probability when the matching advisory guide is present (1.0 = guides do nothing, 0.6 = the published default, 0.0 = a guide fully removes its failure mode). Error scale = multiplier on every simulated hazard error rate of both tiers (1.0 = the published profile). Sealed final accuracy across seeds: mean [min–max].

| Error scale | Guide effect | fixed | ratchet | prompt_only | random | Ratchet > prompt-only (p<0.05) |
|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | 0.6 | 59.1% [52–63] | 90.8% [87–96] | 59.1% [52–63] | 85.4% [81–91] | 5/5 |
| 1.5 | 0.6 | 24.4% [20–31] | 81.5% [76–84] | 24.4% [20–31] | 53.9% [37–72] | 5/5 |
