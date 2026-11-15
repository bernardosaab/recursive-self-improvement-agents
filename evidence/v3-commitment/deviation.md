# Deviation from the pre-registered plan

Recorded before any improvement-loop result was seen.

**Planned:** `small_loop` on offsets base+1..3 with small `qwen2.5:0.5b`, large `qwen2.5:1.5b`, proposer `qwen2.5:7b`.

**Observed first (cross-model check, `evidence/v3-xmodel-*`):** neither `qwen2.5:0.5b` nor `qwen2.5:1.5b` ever cites a record (`references: []` on every answer), so under exact-citation scoring every answer they produce is wrong. A loop whose two model tiers are both always wrong could only measure the zero-model compiler, not the proposer.

**Change:**
- Offset base+1 still runs exactly as pre-registered and is reported.
- Offsets base+2 and base+3 run with small `llama3.2:3b`, large `qwen2.5:7b`, proposer `qwen2.5:7b`. Every other setting is unchanged: arms fixed, ratchet and prompt_only; 8 dev, 15 selection and 15 final tasks per family; 2 cycles.

All runs are reported whatever they show.

## Outcome (added after the runs)

- **Offset base+1, as pre-registered:** completed. See [`evidence/v3-small-loop-seed1/`](../v3-small-loop-seed1/summary.md). The ratchet reached 41.7% against 1.7% for both fixed and prompt-only (24/0, p = 1.2e-7). Its only promotion was the known-path compiler, and the held-out family stayed at 0%, as predicted above.
- **Offsets base+2 and base+3: not completed.** With `llama3.2:3b` and `qwen2.5:7b` both in use, the 8 GB GPU could not keep both models resident (Ollama logged "predicted to exceed available memory, evicting" on every tier switch). Each call reloaded a model, at about 4 s per call. Flash attention and an 8-bit KV cache did not free enough memory. One partial run (about 80 calls) was discarded unread, and the runs were stopped to stay within the session's time budget. No result from them exists or was seen.
