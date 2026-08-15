# Google Gemini Model Comparison (3 Reps Each) — suricata-analyst skill

**Date:** 2026-08-15
**Prompt:** "Give a quick summary of event types and counts in eve-2026-01-21-01.json"
**Skill:** `skills/suricata-analyst`
**Models:** `google:gemini-3.6-flash` vs `google:gemini-3.7-flash`
**Command:** `uv run skill-runner skills/suricata-analyst "<prompt>" --model google:<model> --logfire --debug --pristine`
**Environment:** Google API (GOOGLE_API_KEY), pristine isolated workspaces per run
**Trials:** 3 reps per model, 6 total runs

## Executive summary

Across 3 pristine runs each, the models exhibit markedly different speed profiles and consistency:

- **gemini-3.6-flash:** Consistently fast (5.0–8.4s, avg **6.3s**) ⚡
  - Always uses exactly 2 `run_code` calls (no script persistence)
  - Highly predictable tool usage pattern
  - All 3 runs correct

- **gemini-3.7-flash:** Variable speed (11.2–21.1s, avg **15.4s**) 🐢
  - Always creates a reusable script (`suricata_event_summary.py`)
  - More variable tool call patterns across runs (1–2 run_code calls, sometimes 4 total tool calls)
  - All 3 runs correct

**Speed winner:** gemini-3.6-flash is **~2.4× faster on average** and **~4.2× faster at best case** (5.0s vs 21.1s).

## Correctness

All 6 runs (3 per model) produced **identical, correct numbers**:
- Total events: **428,126** ✓
- Event types: 6-way breakdown identical across all runs
  - flow: 295,239 (68.96%)
  - quic: 76,449 (17.86%)
  - tls: 31,024 (7.25%)
  - dns: 23,555 (5.50%)
  - stats: 1,440 (0.34%)
  - dhcp: 419 (0.10%)

No model got this wrong in any rep. Correctness is not a differentiator.

## Performance across 3 reps

### gemini-3.6-flash

| Rep | Wall time | run_code | list_dir | write_file | Total calls | Correct |
|---|---:|---:|---:|---:|---:|---|
| 1 | **5.6s** | 2 | 1 | 0 | 3 | ✓ |
| 2 | **5.0s** | 2 | 1 | 0 | 3 | ✓ |
| 3 | **8.4s** | 2 | 1 | 0 | 3 | ✓ |
| **AVG** | **6.3s** | 2.0 | 1.0 | 0.0 | 3.0 | ✓ |
| **STDEV** | **1.7s** | 0.0 | 0.0 | 0.0 | 0.0 | — |
| **MIN–MAX** | **5.0–8.4s** | — | — | — | — | — |

**Observations:**
- Extremely consistent — always exactly 2 `run_code`, 1 `list_directory`, 0 `write_file`
- Variation in wall time (5.0–8.4s) is small (~1.7s stdev)
- No script persistence; treats every run as independent
- Tightest execution window of the two models

### gemini-3.7-flash

| Rep | Wall time | run_code | list_dir | write_file | Total calls | Correct |
|---|---:|---:|---:|---:|---:|---|
| 1 | 21.1s | 1 | 1 | 1 | 3 | ✓ |
| 2 | **11.2s** | 2 | 1 | 1 | 4 | ✓ |
| 3 | 13.8s | 1 | 1 | 1 | 3 | ✓ |
| **AVG** | **15.4s** | 1.3 | 1.0 | 1.0 | 3.3 | ✓ |
| **STDEV** | **5.1s** | 0.6 | 0.0 | 0.0 | 0.6 | — |
| **MIN–MAX** | **11.2–21.1s** | — | — | — | — | — |

**Observations:**
- Consistent on script creation (always `write_file`) but variable on execution path
- First run slowest (21.1s), subsequent runs faster (11.2–13.8s)
- Wider variation in wall time (5.1s stdev)
- Inconsistent on `run_code` calls (1–2) and total calls (3–4)
- May benefit from "warming up" or settling on a strategy after first run

## Behavioral differences

### gemini-3.6-flash
- **Strategy:** Minimal exploration; direct computation
- **Tool usage:** Always `list_directory` (state discovery) + 2 `run_code` (schema introspection + aggregation)
- **Script persistence:** None — no `write_file` calls across all 3 reps
- **Speed profile:** Stable, predictable
- **Quality of analysis:** Competent baseline; notes "no alerts," lists event types with percentages
- **Tone:** Direct, concise

### gemini-3.7-flash
- **Strategy:** State discovery + combined aggregation + script creation
- **Tool usage:** Always `list_directory` + 1–2 `run_code` + always `write_file`
- **Script persistence:** Yes — creates `suricata_event_summary.py` every run (even in pristine mode, suggesting the model opts into reusability as a pattern)
- **Speed profile:** Variable, slower on average; first run slowest
- **Quality of analysis:** More elaborate; adds field-level descriptions (e.g., "QUIC protocol handshakes and connection metadata"), explicit note on no-alert scenario, threat-hunting guidance
- **Tone:** Methodical, forward-looking

## Variance analysis

### Speed consistency (coefficient of variation)

- **gemini-3.6-flash:** 1.7s ÷ 6.3s = **0.27 (27% CV)** ← More consistent
- **gemini-3.7-flash:** 5.1s ÷ 15.4s = **0.33 (33% CV)** ← More variable

### Tool usage stability

- **gemini-3.6-flash:** Perfect consistency (identical tool sequence in all 3 reps)
- **gemini-3.7-flash:** Variable (run_code: 1–2, total calls: 3–4); only script creation is fixed

## Takeaways

1. **For speed-critical scenarios:** gemini-3.6-flash wins decisively (avg **6.3s**, predictable, CV 0.27)
2. **For reusability:** gemini-3.7-flash wins (always writes `suricata_event_summary.py`)
3. **For consistency:** gemini-3.6-flash is more predictable (identical tool sequences, tighter variance)
4. **For analysis depth:** gemini-3.7-flash adds more context (field descriptions, threat-hunting pivots)
5. **Trade-off:** Speed/predictability (3.6-flash) vs. elaboration/reusability (3.7-flash). On average, 3.6-flash is **2.4× faster** while remaining fully correct; 3.7-flash's script persistence is valuable only if the workspace persists across runs (not in `--pristine` mode)

## Notes

- **Pristine mode:** Each run had a fresh, isolated workspace with no access to prior artifacts
- **Logfire instrumentation:** Spans sent to Logfire; wall-clock timing extracted from console output
- **No thinking mode:** Baseline analyses without `--thinking` flag
- **Single-run variance:** These are descriptive observations from one run each, but 3 reps per model provide confidence in the speed and tool-usage patterns
- **API consistency:** Both models used `google:` provider (direct Google API), same environment

