# Google Gemini Model Comparison — suricata-analyst skill

**Date:** 2026-08-15
**Prompt:** "Give a quick summary of event types and counts in eve-2026-01-21-01.json"
**Skill:** `skills/suricata-analyst`
**Models:** `google:gemini-3.6-flash` vs `google:gemini-3.7-flash`
**Command:** `uv run skill-runner skills/suricata-analyst "<prompt>" --model google:<model> --logfire --debug --pristine`
**Environment:** Google API (GOOGLE_API_KEY), pristine isolated workspaces per run

Both runs used `--pristine` mode (fresh, isolated task workspace — no prior scripts, reports, or memory).

## Executive summary

Both models correctly identified the event type breakdown with identical numbers, but diverged significantly in speed and methodology:

- **Fastest:** `google:gemini-3.6-flash` at **5.6 seconds** — minimal tool exploration, direct aggregation queries, no script persistence.
- **More elaborate:** `google:gemini-3.7-flash` at **21.1 seconds** — created a reusable baseline script (`suricata_event_summary.py`), added more explanatory context around no-alert scenario and protocol focus.

## Correctness

Both models produced **identical, correct numbers**:
- Total events: 428,126
- Event types: 6-way breakdown (flow 68.96%, quic 17.86%, tls 7.25%, dns 5.50%, stats 0.34%, dhcp 0.10%)
- Same key insight: no direct signature engine `alert` records; threat hunting should focus on protocol metadata pivots

No model got this wrong.

## Behavioral differences

| Model | Approach |
|---|---|
| **gemini-3.6-flash** | Fastest — direct aggregation queries without exploring directory state. No script persistence; relies on inline computation. Minimal but accurate write-up. |
| **gemini-3.7-flash** | Methodical exploration (`list_directory` first) → direct aggregation → script creation. Invested time in writing a reusable baseline script (`suricata_event_summary.py`). Richer context in explanation (named the "no-alert" pivot more explicitly). |

## Performance (observed single runs)

| Model | Wall time | Tool calls | run_code | list_directory | write_file | Correct |
|---|---:|---:|---:|---:|---:|---|
| **gemini-3.6-flash** | **5.6s** | 3 | 2 | 1 | 0 | ✓ |
| **gemini-3.7-flash** | 21.1s | 3 | 1 | 1 | 1 | ✓ |

**gemini-3.6-flash** is **~3.8× faster** than gemini-3.7-flash, despite both completing the task correctly. The speed difference stems from:
- gemini-3.6-flash: two `run_code` calls (describe schema + count types, then total count)
- gemini-3.7-flash: one `run_code` call (combined aggregation), then `write_file` for a reusable script

## Qualitative analysis

### gemini-3.6-flash
- **Strengths:** Fast, efficient, correct numbers, clear tabular output
- **Output quality:** Competent baseline summary; no extraneous exploration. Noted "no `alert` events" but didn't elaborate deeply on implications
- **Script persistence:** None — relies on inline aggregation each time
- **Tone:** Direct, concise, professional

### gemini-3.7-flash
- **Strengths:** Correct numbers, explicit script creation for future reuse, richer contextual explanation
- **Output quality:** More elaborate. Named the "protocol metadata pivot" strategy explicitly; highlighted that flow+quic+tls account for >94% of events. Positioned threat hunting framing around this gap
- **Script persistence:** Yes — created `suricata_event_summary.py` as a reusable baseline
- **Tone:** Methodical, with forward-looking recommendations ("Any threat detection or anomaly hunting should focus on protocol metadata pivots across `dns`, `tls`, `quic`, and flow volume analysis")

## Takeaways

1. **Speed winner:** `google:gemini-3.6-flash` — ~3.8× faster, still correct, minimal overhead
2. **Reusability winner:** `google:gemini-3.7-flash` — persistence of `suricata_event_summary.py` for future runs; richer analysis of the no-alert scenario
3. **Trade-off:** Speed vs. forward-oriented script creation. For one-shot analysis, 3.6-flash wins. For iterative investigation with persistent baselines, 3.7-flash's investment pays off on subsequent runs
4. **Both correct:** No gaps on the actual security question — both correctly pivoted to protocol metadata hunting in the absence of alerts

## Notes on this comparison

- **Single run per model:** These are descriptive observations from one run each, not statistically reliable performance estimates. Latency, tool choices, and write-up depth can vary between runs
- **Isolated workspaces:** Both used `--pristine` mode, so no script reuse from prior sessions — a fair setup for speed measurement
- **No thinking mode:** Neither model was run with `--thinking`; these are baseline analyses
- **Logfire instrumentation:** Spans were sent to Logfire; wall-clock timing extracted from console output, not independently reproducible from local artifacts alone

