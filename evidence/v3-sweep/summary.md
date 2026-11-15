# Offline sensitivity sweep

Seeds [0, 1, 2, 3, 4] (each seed shifts every split seed by 1000×seed and reseeds the simulator); guide effect = multiplier on a failure mode's probability when the matching advisory guide is present (1.0 = guides do nothing, 0.6 = the published default, 0.2 = guides nearly as strong as structural fixes, 0.0 = a guide fully removes the failure mode it targets). Sealed final accuracy across seeds: mean [min–max].

| Guide effect | fixed | ratchet | prompt_only | random | Ratchet > prompt-only (p<0.05) |
|---:|---:|---:|---:|---:|---:|
| 1.0 | 40.1% [36–45] | 86.5% [82–92] | 40.1% [36–45] | 72.0% [51–86] | 5/5 |
| 0.6 | 40.1% [36–45] | 86.5% [82–92] | 40.1% [36–45] | 72.0% [51–86] | 5/5 |
| 0.2 | 40.1% [36–45] | 86.5% [82–92] | 52.6% [45–61] | 72.0% [51–86] | 5/5 |
| 0.0 | 40.1% [36–45] | 86.5% [82–92] | 54.4% [45–71] | 72.0% [51–86] | 5/5 |
