# `--pristine` Model Comparison: Script-Save Habits, Crash Modes, and Duration — gemini-3-flash-preview vs glm-5.2 vs qwen3.6-flash

**Date:** 2026-07-19
**Prompt:** "Analyze this Suricata EVE JSON log for signs of C2 beaconing, anomalous egress, or protocol anomalies. Summarize your findings in markdown."
**Skill:** `skills/suricata-analyst`
**Data:** `eve-2026-01-06-01.json` (184 MB)
**Task:** `--pristine` (fresh, isolated task workspace per run — no prior scripts, reports, or memory)
**Command:** `uv run skill-runner skills/suricata-analyst "<prompt>" --pristine --logfire --model <model>`

Nine runs total: 3 models × 3 reps, identical prompt/data, each in its own isolated `--pristine`
workspace so no run could see or reuse another's saved scripts/reports (unlike the accumulate-mode
comparisons in `openrouter-model-comparison-2026-07-18.md`). Run concurrently in batches of three
(one per model) to control for time-of-day/provider-load effects across reps.

## Executive summary

Four findings, in order of how surprising they were:

1. **The same model, same prompt, same data reached opposite conclusions on the actual security
   question — in both directions.** gemini-3-flash-preview flagged a real, verified 53-hour
   long-lived connection as a likely C2 indicator in 1 of its 3 reps and never mentioned it in the
   other 2. GLM-5.2 investigated the same real, verified MQTT beacon candidate in all 3 of its reps
   but flipped its own verdict — "low risk" in rep 1, "HIGH confidence C2, isolate host" in reps 2
   and 3. See "Qualitative comparison" below — this is outcome variance on the thing the skill
   exists to produce, not just a process/style difference.
2. **Whether a model persists a named, reusable script (vs. only ever using throwaway `run_code`
   snippets) is a real per-model habit, not a coin flip.** GLM-5.2 called `write_file` in every
   single rep; Gemini-3-flash-preview did it in only 1 of 3; qwen3.6-flash never did, across any
   rep. See "Script-save habit" below — this refines the open question in [ISSUES.md](../ISSUES.md) about where
   generated code ends up.
3. **qwen3.6-flash crashed in 2 of 3 reps, each a different failure mode**, and a project-wide
   Logfire/audit-log scan (done as a follow-up to this comparison) found both failure modes had
   already occurred independently in earlier, unrelated sessions — these are systemic weaknesses
   for this model, not flukes of this run. Filed as [ISSUES.md](../ISSUES.md) #5 (tool-retry exhaustion) and #6
   (context-window overflow from an unbounded per-record dump).
4. **GLM-5.2 is genuinely ~5-8x slower than the other two (495-812s vs. 105-166s), while doing a
   similar amount of work** (comparable `run_code` call counts) — it's thorough, not stuck. A
   turn-count safety net wouldn't catch this; only a wall-clock budget would, and none exists in
   `skill_runner` today.

## Results table

| model | rep | duration | `run_code` calls | `write_file` calls | outcome |
|---|---:|---:|---:|---:|---|
| gemini-3-flash-preview | 1 | 143s | 26 | 0 | completed |
| gemini-3-flash-preview | 2 | 105s | 17 | 1 | completed |
| gemini-3-flash-preview | 3 | 117s | 23 | 0 | completed |
| glm-5.2 | 1 | 739s | 26 | 4 | completed |
| glm-5.2 | 2 | 495s | 18 | 2 | completed |
| glm-5.2 | 3 | 812s | 25 | 3 | completed |
| qwen3.6-flash | 1 | 13s | 5 | 0 | **crashed** — tool-retry exhaustion |
| qwen3.6-flash | 2 | 166s | 17 | 0 | completed |
| qwen3.6-flash | 3 | 136s | 20 | 0 | **crashed** — context-window overflow |

Source: `run_start`/`run_code_call`/`tool_call`/`run_end` events in each run's
`workspace/logs/runner-task-*.jsonl` audit log (durations computed from `run_start`→`run_end`
timestamps; these are ground truth, not Logfire-derived).

## Script-save habit: `write_file` vs. `run_code`-only

Two separate mechanisms exist for a model to leave Python behind in the workspace, and they behave
very differently (see [ISSUES.md](../ISSUES.md)'s original write_file/generated_code discussion): a `run_code`
call is always captured automatically, after the fact, as a numbered snapshot under
`generated_code/`, regardless of intent — but a persistent, *named* script only appears if the
model deliberately calls `write_file`. This matrix isolates that choice from workspace-accumulation
effects (every run started from an empty task) and still shows a clear split:

- **glm-5.2**: `write_file` in all 3 reps (4, 2, 3 calls) — every rep, without exception.
- **gemini-3-flash-preview**: `write_file` in 1 of 3 reps (1 call) — inconsistent even for the
  identical prompt/data.
- **qwen3.6-flash**: `write_file` in 0 of 3 reps — always relied on ephemeral `run_code` snippets
  only (when it didn't crash first).

Small sample (3 reps/model), but GLM's 3-for-3 record reads as an actual habit rather than noise,
while Gemini's 1-for-3 looks like genuine per-run variance.

## Qualitative comparison: what did each run actually conclude?

Everything above is structural (call counts, timing, crashes). This section reads the actual
`## Final Findings` write-up from each of the 7 completed runs — the real deliverable a security
analyst would receive — the same way `analyst-log-review-behavior-2026-07-19.md` looked at actual
tool-use behavior rather than just counts. Headline verdict per run:

| model | rep | headline verdict | distinguishing finding |
|---|---:|---|---|
| gemini-3-flash-preview | 1 | **C2 suspected** | 53.4h flow `192.168.2.197→192.200.0.106:80` — verified real (below), unique to this run |
| gemini-3-flash-preview | 2 | No C2 found | — |
| gemini-3-flash-preview | 3 | No C2 found | — |
| glm-5.2 | 1 | No C2 found | examined `192.168.3.105→20.44.17.102:8883`, cleared as "likely Azure IoT/notification — low risk" |
| glm-5.2 | 2 | **C2 suspected (HIGH confidence)** | same `20.44.17.102:8883` MQTT beacon, "isolate host... full PCAP... host IR for implant" |
| glm-5.2 | 3 | **C2 suspected (HIGH severity)** | same beacon again, "textbook C2 beacon profile," same isolate/PCAP recommendation |
| qwen3.6-flash | 2 | No C2 found | flagged an unrelated Cloudflare UDP stream as its top "HIGH" anomaly — a benign CDN download pattern |

Two things stand out, and both check out against the raw log, not just the model's say-so:

**The 53-hour flow is real, and only one run out of nine ever surfaced it.** gemini-rep1's
headline finding was a single `flow` event, `192.168.2.197 → 192.200.0.106:80`, with a reported
53.4-hour duration starting three days before the log file's own nominal 24-hour capture window.
That's an odd enough claim to check rather than trust:

```
$ grep "192.200.0.106" workspace/data/eve-2026-01-06-01.json | wc -l
1
$ grep "192.200.0.106" workspace/data/eve-2026-01-06-01.json | python3 -c "..."
2026-01-06T01:32:49 flow 192.168.2.197 192.200.0.106 80  flow.start=2026-01-03T20:05:58  flow.end=2026-01-06T01:31:44  age=192346
```

It checks out exactly — one real `flow` record, `age: 192346` seconds (53.4h), `flow.start` three
days before the event's own logged `timestamp`, meaning the underlying TCP connection was already
in progress when the capture window began and only got flushed/logged near the end of the file.
This is a legitimate, rare (1-in-227,732-flow-records), genuinely interesting artifact for a
beaconing/persistence hunt — and 8 of 9 runs across all three models never mention it at all,
including the *other two* gemini-3-flash-preview reps against the identical prompt/data. Whatever
query/aggregation approach surfaced this for gemini-rep1 wasn't reproduced by its own later reps.

**The MQTT beacon is real too (confirmed: exactly 125 flows, only from `192.168.3.105`, only to
port 8883), and only GLM-5.2 ever investigates it** — but GLM's own verdict on the *identical*
signal flips between reps. Rep 1 explicitly checks it and calls it "likely Azure IoT/notification
— low risk." Reps 2 and 3 independently rank it "HIGH confidence"/"HIGH severity" C2 and recommend
host isolation and PCAP capture. All three reps clearly ran the same kind of systematic
payload-consistency/timing-regularity beacon scan (rep 3 even names it: "a scan of all flow
tuples... ranked this connection as the #1 beaconing candidate") — the scan finds the same
candidate every time, but the model's own severity judgment on what it found is not stable
run-to-run. Gemini and qwen never surface this candidate at all in any of their reports, which
suggests it's specifically GLM's beacon-scanning methodology (not just luck) that surfaces it —
GLM just doesn't agree with itself about what to do once it's found.

**Everything else was consistent.** All 7 reports independently identified the same Tailscale
mesh VPN traffic (same DERP relay pattern, same `_grpc_config.localhost.tail4ee2.ts.net` DNS
signature, same STUN port 3478 explanation) and the same handful of high-egress-but-benign hosts
(Adobe Creative Cloud, OneDrive/Microsoft 365, iCloud, gaming platforms) as background noise,
correctly dismissed. The disagreement is concentrated entirely in the two genuinely ambiguous,
low-volume, rare signals above — exactly the kind of finding a single-run analysis would have no
way of knowing was unstable.

**Why this matters more than the write_file/duration findings above:** those were process
differences. This is outcome variance on the actual security question the skill exists to answer
— whether the same model, given the identical prompt and data, reports a genuine anomaly as the
top finding depends on which run you happened to get, in both directions (gemini finding it once
and not twice; GLM finding it every time but disagreeing with itself about severity).

## Crash 1 — qwen3.6-flash, rep 1: tool-retry exhaustion (13s)

```
pydantic_ai.exceptions.UnexpectedModelBehavior: Tool 'run_code' exceeded max retries count of 3.
```

The model wrote `p.stat().st_size:,` (comma thousands-separator format specifier) and then
`"...".format(...)` across 3 consecutive `run_code` calls — both unsupported in Monty — and never
landed on working syntax before the retry cap cut it off. Task:
`task-37baec46-8e50-4f8c-bb9f-822d989e3d27`. Full detail, plus a second independent occurrence of
the identical `{x:,}` mistake from an earlier, unrelated session: [ISSUES.md](../ISSUES.md) #5.

## Crash 2 — qwen3.6-flash, rep 3: context-window overflow (136s)

```
pydantic_ai.exceptions.ModelHTTPError: ... maximum context length is 1000000 tokens. However,
you requested about 6654694 tokens ...
```

Cause: a `run_code` call collected all 1,440 `event_type == "stats"` records into a list and
printed each one with `json.dumps(s, indent=2)` — no sampling, no aggregation — producing a
24,738,153-char tool return that got fed straight back into the model's own context. Task:
`task-c3acb612-c3e6-4c13-9994-ccd9e4373ffa`. A project-wide scan afterward found the *identical*
pattern (same event type, same unbounded-dump shape, same ~23.7M-char return) in a separate,
earlier `suricata-triage` session — see [ISSUES.md](../ISSUES.md) #6 for both transcripts side by side.

Notably, neither Gemini-3-flash-preview nor GLM-5.2 produced a `run_code` return anywhere near this
size against the identical prompt/data, in any rep — this looks like a `qwen3.6-flash`-specific
blind spot rather than something the prompt/data provoke generically.

## Duration: GLM-5.2 is slow, not stuck

GLM-5.2's 3 reps (739s, 495s, 812s) took 5-8x longer than Gemini's (143s, 105s, 117s) or qwen's
successful reps (166s), despite making a comparable number of `run_code` calls (25-26 vs.
Gemini's 17-26) — i.e., it isn't looping or stuck, it's just a slower model doing similar-shaped
work. This came up mid-comparison when GLM's rep 3 was still running well past the point every
other rep across every model had already finished; watching it live in Logfire showed steady,
non-repeating `chat`/`run_code` progress the whole time, not a stall.

This directly motivates adding a wall-clock run budget to `skill_runner` (`--max-run-seconds`, no
current equivalent) as distinct from a turn-count cap (`pydantic_ai`'s existing
`UsageLimits.request_limit`, default 50 — GLM's busiest rep only used 16 of that budget, nowhere
close to tripping) or a retry-count cap (`CodeMode.max_retries`, relevant to crash 1 above, not to
this). See the CLI-design discussion this comparison prompted, still pending implementation as of
this writing.

## A Logfire terminology note

While this comparison was live, GLM-5.2's Logfire trace showed 51 total spans, which reads as
"over 50 turns" at a glance. It isn't: `chat z-ai/glm-5.2` (actual model requests) accounted for
only 16 of those 51 — the rest are `execute_tool run_code`/`write_file`/`read_file`/
`list_directory` spans nested under the same trace. `pydantic_ai`'s own `request_limit` (the
thing that would actually throttle "too many turns") only counts the `chat` spans, so this trace
was nowhere near its limit despite looking busy in the Logfire UI's total span count.

## Limitations

- 3 reps per model is enough to see a pattern (GLM's 3-for-3 `write_file`, qwen's 2-for-3 crash
  rate) but not enough for a statistically reliable rate estimate.
- Only 3 of the ~15 models in `models.yaml` were tested here; unknown how this generalizes to the
  rest of the roster.
- Cost/token telemetry from Logfire only matched a subset of `chat` spans per trace (missing
  `gen_ai.usage` attributes on some, and none at all for qwen's rep-1 crash, likely because the
  abnormal exception path skips normal span-attribute finalization) — this note relies on the
  local audit-log timestamps and event counts as ground truth instead, not Logfire-derived
  duration/cost figures.
- Same single-prompt caveat as the 2026-07-18 comparison: one prompt shape (open-ended "find
  anomalies"), not a suite — behavior may differ for narrower, more deterministic asks.
- The qualitative comparison verified that the two disputed signals (the 53h flow, the MQTT
  beacon) are *real records in the log*, matching the reports' own numbers exactly — it does
  **not** establish that either is actually malicious. This is a synthetic/lab capture with no
  independently-known ground truth about what's really C2 versus a mundane IoT/telemetry device;
  "real signal that models disagree about" is the finding, not "confirmed compromise."
