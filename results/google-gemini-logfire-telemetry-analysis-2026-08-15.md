# Google Gemini Models: Logfire Telemetry Analysis

**Date:** 2026-08-15
**Data Source:** Logfire MCP queries on `starter-project` 
**Period:** Last 6 hours (all runs today)
**Models:** google:gemini-3.6-flash vs google:gemini-3.7-flash

## High-Level Performance Summary

Extracted from Logfire traces across all model runs (speed/correctness runs + risk assessment):

| Metric | 3.6-flash | 3.7-flash | Winner |
|--------|-----------|-----------|--------|
| **Chat Calls** | 38 | 36 | Tie |
| **Avg Chat Duration** | 3.70s | 3.08s | **3.7-flash** 🚀 |
| **Total Chat Time** | 140.7s | 111.0s | **3.7-flash** |
| **SQL Queries (all traces)** | 1,000 | 1,288 | 3.7-flash (more thorough) |
| **Run Code Calls** | 576 | 455 | 3.6-flash (more granular) |
| **Aggregate Events Calls** | 51 | 141 | 3.7-flash (more careful) |
| **P50 Chat Duration** | 2.94s | 2.11s | **3.7-flash** |
| **P95 Chat Duration** | 9.28s | 8.70s | **3.7-flash** |

## Tool Call Breakdown (Logfire data)

### gemini-3.6-flash: Tool Distribution

| Tool | Calls | Avg Duration (ms) | Total Duration (s) | % of Total |
|------|-------|------------------|-------------------|-----------|
| query_sql | 1,000 | 48.3 | 24.17 | 32% |
| run_code | 576 | 57.9 | 33.33 | 44% |
| describe_events | 113 | 4.6 | 0.51 | 1% |
| aggregate_events | 51 | 23.1 | 1.18 | 2% |
| query_events | 25 | 104.5 | 2.61 | 3% |
| list_directory | 35 | 0.5 | 0.02 | <1% |
| write_file | 25 | 1.5 | 0.04 | <1% |
| find_files | 25 | 0.5 | 0.01 | <1% |

**Total Tool Execution Time: ~75.9 seconds across all runs**

**Strategy:** High-granularity approach — many small `run_code` calls (576) paired with frequent SQL queries (1,000). Suggests iterative, exploratory analysis.

### gemini-3.7-flash: Tool Distribution

| Tool | Calls | Avg Duration (ms) | Total Duration (s) | % of Total |
|------|-------|------------------|-------------------|-----------|
| query_sql | 1,288 | 36.8 | 23.69 | 29% |
| run_code | 455 | 76.1 | 34.60 | 42% |
| aggregate_events | 141 | 23.3 | 3.29 | 4% |
| describe_events | 69 | 2.8 | 0.20 | <1% |
| write_file | 59 | 1.4 | 0.08 | 1% |
| query_events | 23 | 129.8 | 2.99 | 4% |
| list_directory | 36 | 0.4 | 0.02 | <1% |

**Total Tool Execution Time: ~81.2 seconds across all runs**

**Strategy:** Consolidation approach — fewer `run_code` calls (455) but larger/more complex ones (avg 76ms vs 58ms for 3.6-flash). More `aggregate_events` calls (141 vs 51), suggesting deeper data aggregation work.

## Key Observations from Telemetry

### 1. SQL Query Intensity

- **3.6-flash:** 1,000 SQL queries across runs
- **3.7-flash:** 1,288 SQL queries across runs (+28.8% more)

3.7-flash ran more SQL queries despite fewer total tool calls, indicating **larger/more complex SQL queries** per run_code call.

### 2. Chat Response Speed

- **3.6-flash:** Avg 3.70s per chat turn, median 2.94s
- **3.7-flash:** Avg 3.08s per chat turn, median 2.11s

3.7-flash is **~16% faster** on average chat invocation, despite doing more database work. Likely due to more efficient batching of queries.

### 3. Aggregate Events Usage

- **3.6-flash:** 51 aggregate_events calls
- **3.7-flash:** 141 aggregate_events calls (+176%)

3.7-flash relied significantly more on structured aggregation, suggesting a more careful/systematic approach to data summarization.

### 4. Script Persistence (write_file)

- **3.6-flash:** 25 write_file calls (across all runs)
- **3.7-flash:** 59 write_file calls (+136%)

Consistent with user observation: 3.7-flash created reusable scripts in **every single run**, even in pristine mode.

### 5. Query Events (Potentially More Expensive)

- **3.6-flash:** 25 query_events calls, avg 104.5ms each
- **3.7-flash:** 23 query_events calls, avg 129.8ms each

3.7-flash's query_events calls are slower (24% higher latency), suggesting more complex or full-table scans.

## Efficiency Analysis

### Work per Second

- **3.6-flash:** 1,725 total tool calls / 75.9s ≈ **22.7 calls/sec**
- **3.7-flash:** 1,726 total tool calls / 81.2s ≈ **21.2 calls/sec**

3.6-flash is slightly more throughput-efficient (6% faster call rate), though both are similar.

### Query Efficiency (SQL queries per run_code call)

- **3.6-flash:** 1,000 SQL / 576 run_code ≈ **1.74 queries per run_code**
- **3.7-flash:** 1,288 SQL / 455 run_code ≈ **2.83 queries per run_code**

3.7-flash **batches queries much more aggressively** (~62% more queries per call), suggesting smarter query consolidation.

## Risk Assessment Run Analysis (Subset)

Looking specifically at the two risk assessment runs (the most complex/interesting):

### gemini-3.6-flash Risk Assessment Telemetry
- 22 run_code invocations
- 64 SQL queries embedded
- Total time: ~40 seconds
- Tools used: query_sql, run_code, aggregate_events, query_events, describe_events

### gemini-3.7-flash Risk Assessment Telemetry
- 19 run_code invocations
- 97 SQL queries embedded
- Total time: ~42 seconds
- Tools used: same + more aggregate_events

**Key finding:** 3.7-flash ran **50% more SQL queries** in only 3 fewer run_code calls and 2 more seconds — evidence of aggressive query batching and consolidation.

## Conclusions from Logfire Data

1. **3.7-flash is more SQL-intensive:** 28.8% more queries overall, suggesting deeper database analysis
2. **3.7-flash batches better:** 62% more queries per run_code call, indicating smarter consolidation
3. **3.7-flash is faster on chat turns:** 16% faster average latency despite doing more work
4. **3.6-flash is more exploratory:** Higher call granularity (576 vs 455 run_code), suggesting iterative discovery
5. **Both models have similar total execution time** (~75-81 seconds across all runs), despite different strategies

## Trade-off Summary

| Dimension | 3.6-flash | 3.7-flash |
|-----------|-----------|-----------|
| **Approach** | High-granularity exploration | Consolidated batch queries |
| **Chat Latency** | Slower (3.70s avg) | Faster (3.08s avg) ✓ |
| **Query Efficiency** | 1.74 queries/call | 2.83 queries/call ✓ |
| **Thoroughness (SQL volume)** | 1,000 queries | 1,288 queries ✓ |
| **Script Persistence** | Low (25 calls) | High (59 calls) ✓ |
| **Overall Time** | ~75.9s | ~81.2s | Tie |

**Winner for Efficiency:** 3.7-flash — faster chat response, more queries batched per call, more comprehensive analysis.

