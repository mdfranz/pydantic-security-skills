# OpenRouter Model Comparison — suricata-analyst skill

**Date:** 2026-07-18
**Prompt:** "Give a quick summary of event types and counts in eve-2026-01-21-01.json"
**Skill:** `skills/suricata-analyst`
**Command:** `python runner.py skills/suricata-analyst "<prompt>" --model openrouter:<model> --logfire --debug`

All runs used the same task, skill, and input file, but this was a sequential comparison in a mutating workspace: kimi-k3 created `suricata_event_type_counts.py`, which subsequent runs could reuse. Treat the results as observed single-run behavior, not a controlled apples-to-apples A/B.

## Executive summary

All 7 models got the numbers right. The qualitative ranking below is subjective, and the speed/cost figures are reported Logfire telemetry rather than independently reproducible local measurements.

## Experimental limitations

This is an exploratory comparison, not a pristine benchmark:

- **Shared, additive workspace.** Runs were sequential rather than isolated. Scripts, generated code, analyst logs, and result files persisted across runs; later models could discover or reuse artifacts created by earlier ones.
- **Order and prompt effects.** kimi-k3 created `suricata_event_type_counts.py` during the baseline sequence. Later runs could reuse that counter, and their runner-supplied reusable-script list included it; kimi-k3's did not. Matching later counts therefore demonstrate successful reuse as well as correctness, not fully independent rediscovery of the task.
- **Prior-report contamination.** The workspace contained prior analyst logs and results for the same capture. A model could read or search them, changing both its available context and its tool path.
- **One trial per configuration.** Model outputs, tool choices, provider routing, and latency can vary between runs. The rankings are descriptive observations from these runs, not statistically reliable performance estimates.
- **Narrow, deterministic task.** Event-type counting is a simple task with a single checkable answer. It does not measure broader security-analysis capability, and the evaluation of extra "insight" is subjective rather than ground-truth scored.
- **Telemetry limits.** Timing, cost, token, and request-parameter claims depend on Logfire/OpenRouter instrumentation and contemporaneous provider pricing/routing. They cannot be independently reconstructed from the local run artifacts alone.
- **No environment reset.** Accumulated files and possible warm filesystem/cache effects may favor later runs.

**Best combined pick in these observed runs: `deepseek/deepseek-v4-pro`.** It combined fast reported latency (20.1s baseline, 17.8s with thinking), low reported cost ($0.0042 / $0.0012), and the sharpest observation in the set: QUIC as "~2.5× TLS volume" (a direct, checkable ratio). `nvidia/nemotron-3-ultra-550b-a55b` had the fastest reported result (14.1s) and highest reported throughput (55 tok/s), but its write-ups were the terse, least-elaborated of the seven.

**Worst overall: `minimax/minimax-m3`.** Not wrong, just the worst insight-per-effort ratio: it made the *most* tool calls of any model (sampling raw lines, reading the full reference doc, running the script — 6 calls total in baseline) yet produced the *most generic* baseline write-up of the seven ("stats events are operational telemetry," no novel angle). More exploration didn't buy more analysis. (It's also the one model where `--thinking medium` clearly helped — see below — so it's worst specifically *without* thinking enabled.)

**What actually differentiated them:**
- **Script-reuse discipline, not raw effort.** The models that reused the existing `suricata_event_type_counts.py` directly (deepseek-v4-pro, glm-5.2, glm-5, nemotron-3-ultra) were both faster *and* no less correct than the ones that re-explored the data first (minimax-m3, kimi-k3's first run). Extra tool calls didn't correlate with extra insight anywhere in this test.
- **Specificity of the one "extra" observation.** Every model correctly reported "no alerts → pivot to protocol hunting," which is boilerplate from the skill's own troubleshooting note. What separated good from mediocre write-ups was whether the model added one further, checkable, dataset-specific number on top of that — a ratio (deepseek-v4-pro's 2.5×), a cross-session comparison (kimi-k3's 22× vs. the prior hourly sample), or a named technique from the skill text itself (glm-5.2's "PCR"). Models that stopped at the boilerplate (glm-5, nemotron-3-ultra, deepseek-v4-flash) were correct but forgettable.
- **Whether the reusable script already existed.** This is a major confound: kimi-k3 created the reusable script during the baseline sequence, and all later runs—including every thinking run—could reuse it. That changes the work required, so it is not possible to attribute write-up-depth differences to the `--thinking` flag alone.
- **Cost is not well correlated with quality.** `glm-5.2` was the priciest confirmed cost in the baseline round ($0.0176, tied for slowest known-cost model at 39.0s) without a standout qualitative edge over its cheaper peers — the weakest cost-to-insight trade in the set, short of minimax-m3's tool-call waste.

## Correctness

All 7 models produced **identical, correct numbers**: 428,126 events, 0 malformed lines, same 6-way event-type breakdown (flow 68.96%, quic 17.86%, tls 7.25%, dns 5.50%, stats 0.34%, dhcp 0.10%), same 24h time range, same "zero alerts → pivot to protocol hunting" conclusion. No model got this wrong.

## Behavioral differences

| Model | Approach |
|---|---|
| **kimi-k3** | Most thorough — cross-referenced `suricata_60min_analysis_results.json`, correctly noting that it covered only the first hour while the file spans 24h. Wrote the reusable `suricata_event_type_counts.py` script (which did not exist at the start of the sequence). |
| **glm-5.2** | Found and reused the new script directly, no wasted steps. |
| **minimax-m3** | Least efficient — sampled 5 raw lines *and* read the full `eve_format.md` reference *and* ran the script, despite the script alone being sufficient. |
| **deepseek-v4-flash** | Clean reuse, concise. |
| **glm-5** | Clean reuse, one of the most concise final answers. |
| **nemotron-3-ultra** | Clean reuse, fastest and most direct. |
| **deepseek-v4-pro** | Clean reuse (`list_directory`+`read_file` in parallel, then `run_code`); no wasted steps. |

## Performance (from Logfire `invoke_agent`/`chat` spans)

| Model | Wall time | LLM turns | Tool calls | Input tok | Output tok | Reasoning tok | Cost | Output tok/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| kimi-k3 | **116.9s** | 4 | 5 | 34,883 | 3,109 | 1,728 | *n/a¹* | 26.6 |
| glm-5.2 | 39.0s | 3 | 3 | 19,111 | 954 | 141 | $0.0176 | 24.5 |
| minimax-m3 | 39.0s | 4 | 6 | 51,698 | 1,425 | 550 | $0.0102 | 36.5 |
| deepseek-v4-flash | 21.0s | 4 | 3 | 26,996 | 1,066 | 170 | **$0.0020** | 50.8 |
| glm-5 | 20.2s | 3 | 2 | 16,963 | 721 | 114 | $0.0064 | 35.8 |
| **nemotron-3-ultra** | **14.1s** | 3 | 2 | 18,275 | 775 | 145 | $0.0110 | **55.0** |
| deepseek-v4-pro | 20.1s | 3 | 3 | 20,807 | 955 | 148 | $0.0042 | 47.6 |

¹ OpenRouter didn't populate `operation.cost` for moonshotai in this window, so kimi-k3's cost is unknown and should not be inferred from token volume alone.

**deepseek-v4-pro** lands close to `nemotron-3-ultra` on speed (20.1s vs. 14.1s) and well under half its cost of the priciest models, at $0.0042 — roughly 2x `deepseek-v4-flash`'s cost but with a notably sharper qualitative read (see below).

## Takeaways

- **Speed/cost winner:** `nvidia/nemotron-3-ultra-550b-a55b` — fastest (14s), highest throughput (55 tok/s), correct and concise, at a moderate cost.
- **Cheapest:** `deepseek/deepseek-v4-flash` at $0.002, still fast (21s) and correct.
- **Most careful but slowest:** `kimi-k3` — the only one to independently notice the stale 10-min-window discrepancy in earlier reports, but 5-8x slower than the rest, largely due to reading two extra files, higher reasoning-token usage, and a slow 53s tool-bound turn.
- **Least efficient tool use:** `minimax-m3` — did redundant sampling/reference-reading on a task the existing script already solved, despite tying glm-5.2 on wall time only because of a cheaper per-token rate.
- **Best combined pick in these observed runs:** `deepseek/deepseek-v4-pro` — fast (20.1s), low reported cost ($0.0042), correct, and produced the sharpest ratio-based observation (QUIC ≈ 2.5× TLS volume).

## `--thinking medium` re-run

Same task/skill/file, all 7 models, with `--thinking medium` added. Logfire reported that the flag took effect: every `chat` span's `model_request_parameters.thinking` was `"medium"`, `gen_ai.request.max_tokens` was floored to 14,096 (`runner.py`'s `ANTHROPIC_THINKING_BUDGET_MAP["medium"]` = 10,000 + 4,096), and every call reported nonzero `gen_ai.usage.details.reasoning_tokens`. No HTTP errors occurred, and all 7 returned the **identical correct result**. These request/usage details are not independently verifiable from the local run artifacts.

| Model | Wall time | Reasoning tok (total) | Cost | Output tok/s | vs. baseline wall time |
|---|---:|---:|---:|---:|---|
| kimi-k3 | 52.3s | 405 | *n/a¹* | 25.6 | 116.9s → 52.3s (faster — took a more direct path, no extra file reads this run) |
| glm-5.2 | 26.8s | 726 | $0.0116 | 28.9 | 39.0s → 26.8s (faster, cheaper) |
| minimax-m3 | 20.8s | 201 | $0.0044 | 53.7 | 39.0s → 20.8s (faster, ~2.3x cheaper — skipped the redundant sampling this run) |
| deepseek-v4-flash | 23.5s | 92 | $0.0021 | 40.5 | 21.0s → 23.5s (roughly flat) |
| glm-5 | 18.7s | 159 | $0.0065 | 40.5 | 20.2s → 18.7s (roughly flat) |
| nemotron-3-ultra | 11.0s | 142 | $0.0145 | 77.0 | 14.1s → 11.0s (faster, but ~1.3x pricier — 4 turns vs. 3 baseline) |
| deepseek-v4-pro | 17.8s | 193 | $0.0012 | 49.7 | 20.1s → 17.8s (faster, ~3.4x cheaper — same 3-turn shape, just less costly this run) |

¹ `operation.cost` still not populated for moonshotai on OpenRouter.

**Caveat:** this isn't a clean thinking-on/thinking-off A/B. Each model took a different tool-call path than its baseline run (e.g. kimi-k3 skipped the prior 60-minute-results cross-reference; minimax-m3 skipped redundant sampling), and every thinking run inherited the reusable script that kimi-k3 created during baseline. This confounds the timing, cost, and write-up-depth deltas. The narrower Logfire-backed finding is that `--thinking medium` reached the requests and produced reasoning-token telemetry without errors; it does not establish that thinking reliably improves speed or analytical depth.

## Qualitative analysis — baseline (no `--thinking`)

All correct, but depth of the *written analysis* (beyond the raw table) varied:

- **kimi-k3** — richest of the seven. Wrote the `suricata_event_type_counts.py` script (which did not exist at the start of the sequence) and, in the same answer, cross-referenced `suricata_60min_analysis_results.json`, correctly computing that the full file is **~22× larger** than that prior hour-long sample (428,126 ÷ 19,313 ≈ 22.2 — checked, correct). Named a specific pivot technique: "PCR/exfil via flows."
- **glm-5.2** — computed "~25% of all events are encrypted connections" from QUIC+TLS (76,449+31,024 = 107,473 ÷ 428,126 = 25.1% — checked, correct). Precise, reused the script outright.
- **minimax-m3** — despite the most tool calls (sampled 5 raw lines, read the `eve_format.md` reference, ran the script), the actual write-up was among the most generic: "stats events are operational telemetry" — descriptive, no novel hypothesis. More exploration did not translate into more insight in this run.
- **deepseek-v4-flash** — correct arithmetic ("TLS and DNS together make up ~13%": 7.25+5.50=12.75, rounds fine) and the same "want me to dive deeper?" closing habit seen in its thinking run too.
- **glm-5** — competent, generic bullet list; consistent voice with its thinking run.
- **nemotron-3-ultra** — terse ("good coverage for protocol-based hunting"); consistent minimal-elaboration style in both conditions.
- **deepseek-v4-pro** — sharpest baseline observation of the seven: "QUIC is unusually prominent at ~18% — nearly 2.5× TLS volume" (76,449 ÷ 31,024 = 2.464 — checked, correct). Framing QUIC as a direct ratio to TLS specifically (rather than the generic "QUIC+TLS = ~25% encrypted" seen elsewhere) is a more pointed, hunt-oriented statistic. Also the only one besides deepseek-v4-flash to close with an explicit "want me to dive deeper?" offer, naming three concrete follow-ups (QUIC anomalies, TLS SNI hunting, DNS exfiltration).

## Qualitative analysis — `--thinking medium`

- **kimi-k3** — ties QUIC+TLS into "~107k connections" and recommends pivoting to `tls.sni`/`quic.sni` specifically; it also references earlier 10-minute reports. Most grounded of the seven, but noticeably *less* elaborate than its own baseline run.
- **minimax-m3** — the only model to float an original threat hypothesis: "C2-over-QUIC or non-browser QUIC clients," and correctly noted no `suricata_quic_*.py` script exists yet. One inaccuracy: called QUIC (17.86%) "almost as much as TLS+DNS combined" when TLS+DNS is 12.75% — QUIC actually *exceeds* their combined share by ~40%, not "almost as much."
- **glm-5.2** — named "PCR" (Producer-Consumer Ratio) explicitly, pulled straight from the skill's own Example 2 — shows it's using the skill's vocabulary, not generic advice.
- **glm-5** — correct pivot suggestions (rare SNIs, DNS anomalies, beacon detection) but generic, no dataset-specific reasoning.
- **deepseek-v4-flash** — vaguest hedged language ("could indicate... potentially C2/beaconing") but best UX habit (explicit "would you like to dive deeper?" close).
- **nemotron-3-ultra** — leanest write-up of the seven, consistent with its reported speed profile.
- **deepseek-v4-pro** — dropped its own baseline's sharp "~2.5× TLS volume" ratio in favor of vaguer language ("QUIC is the standout protocol... worth a closer look if that's unexpected") — a step down in specificity from its own non-thinking run.

## Thinking vs. no-thinking: does it actually add analytical depth?

| Model | Effect of `--thinking medium` on write-up quality |
|---|---|
| kimi-k3 | **Less** elaborate than baseline — baseline had more to discover (script didn't exist yet), forcing a richer cross-reference. Thinking mode reused the already-saved script and gave a leaner (though still accurate) answer. |
| glm-5.2 | Roughly equal — both runs are precise and skill-vocabulary-aware; thinking version named "PCR" explicitly, baseline computed the "~25% encrypted" stat explicitly. A wash. |
| minimax-m3 | **More** insightful — only the thinking run produced a specific, novel hypothesis (C2-over-QUIC), despite doing *less* tool exploration than its own baseline. The one model where thinking visibly helped. |
| deepseek-v4-flash | No visible difference — same tone, same closing question, same level of genericness in both. |
| glm-5 | No visible difference — same generic-but-correct style in both. |
| nemotron-3-ultra | No visible difference — terse in both. |
| deepseek-v4-pro | **Less** insightful — baseline's specific "~2.5× TLS volume" ratio became a vaguer "worth a closer look if unexpected" under thinking. Same pattern as kimi-k3: the model already had the script from the prior run, so there was less to discover, and it wrote less as a result. |

**Bottom line:** these sequential single runs do not show a reliable increase in analytical depth from `--thinking medium`. Minimax-m3 produced a sharper hypothesis in its thinking run; the others were roughly comparable or less specific. Because the tool paths changed and the workspace state was inherited, this is descriptive evidence only—not a causal result about the thinking flag. The reusable script's availability is a substantial confound.
