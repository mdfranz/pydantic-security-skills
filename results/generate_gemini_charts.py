#!/usr/bin/env python3
"""Generate PNG charts for Google Gemini model evaluation report."""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path

# Set style
plt.style.use('seaborn-v0_8-darkgrid')
colors = {
    '3.6-flash': '#0D7A8E',
    '3.7-flash': '#CB2431'
}

results_dir = Path(__file__).parent

# Chart 1: Speed Performance (3 runs)
fig, ax = plt.subplots(figsize=(10, 6))
runs = ['Run 1', 'Run 2', 'Run 3']
flash_36 = [5.6, 5.0, 8.4]
flash_37 = [21.1, 11.2, 13.8]

x = np.arange(len(runs))
width = 0.35

bars1 = ax.bar(x - width/2, flash_36, width, label='3.6-flash', color=colors['3.6-flash'], alpha=0.8)
bars2 = ax.bar(x + width/2, flash_37, width, label='3.7-flash', color=colors['3.7-flash'], alpha=0.8)

ax.set_xlabel('Run', fontsize=11, fontweight='bold')
ax.set_ylabel('Duration (seconds)', fontsize=11, fontweight='bold')
ax.set_title('Speed Performance: Event Counting Task (3 Repetitions)', fontsize=13, fontweight='bold', pad=20)
ax.set_xticks(x)
ax.set_xticklabels(runs)
ax.legend(fontsize=10)
ax.grid(axis='y', alpha=0.3)

# Add value labels on bars
for bars in [bars1, bars2]:
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.1f}s', ha='center', va='bottom', fontsize=9)

plt.tight_layout()
plt.savefig(results_dir / 'gemini-speed-performance.png', dpi=150, bbox_inches='tight')
plt.close()

# Chart 2: Speed Consistency & Average
fig, ax = plt.subplots(figsize=(10, 6))
models = ['3.6-flash', '3.7-flash']
averages = [6.3, 15.4]
min_vals = [5.0, 11.2]
max_vals = [8.4, 21.1]

x = np.arange(len(models))
width = 0.35

bars = ax.bar(x, averages, width, label='Average', color=[colors['3.6-flash'], colors['3.7-flash']], alpha=0.8)

# Add error bars for min/max range
errors_low = [averages[i] - min_vals[i] for i in range(len(models))]
errors_high = [max_vals[i] - averages[i] for i in range(len(models))]
ax.errorbar(x, averages, yerr=[errors_low, errors_high], fmt='none', color='black',
            capsize=5, capthick=2, label='Min-Max Range', linewidth=2)

ax.set_ylabel('Duration (seconds)', fontsize=11, fontweight='bold')
ax.set_title('Speed Consistency: Average vs Range', fontsize=13, fontweight='bold', pad=20)
ax.set_xticks(x)
ax.set_xticklabels(models)
ax.legend(fontsize=10)
ax.grid(axis='y', alpha=0.3)

# Add value labels
for i, (bar, avg) in enumerate(zip(bars, averages)):
    ax.text(bar.get_x() + bar.get_width()/2., avg + 0.5,
            f'{avg:.1f}s', ha='center', va='bottom', fontsize=10, fontweight='bold')

plt.tight_layout()
plt.savefig(results_dir / 'gemini-speed-consistency.png', dpi=150, bbox_inches='tight')
plt.close()

# Chart 3: Query Efficiency
fig, ax = plt.subplots(figsize=(10, 6))
models = ['3.6-flash', '3.7-flash']
queries_per_call = [1.74, 2.83]

bars = ax.barh(models, queries_per_call, color=[colors['3.6-flash'], colors['3.7-flash']], alpha=0.8, height=0.5)

ax.set_xlabel('Queries per run_code Call', fontsize=11, fontweight='bold')
ax.set_title('Query Efficiency: Batching Ratio (Higher = Better)', fontsize=13, fontweight='bold', pad=20)
ax.grid(axis='x', alpha=0.3)

# Add value labels
for i, (bar, val) in enumerate(zip(bars, queries_per_call)):
    ax.text(val + 0.05, bar.get_y() + bar.get_height()/2.,
            f'{val:.2f}', ha='left', va='center', fontsize=11, fontweight='bold')

# Add annotation for efficiency gain
ax.text(2.3, 0.5, '62% better\nbatching', ha='center', fontsize=9,
        bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.3))

plt.tight_layout()
plt.savefig(results_dir / 'gemini-query-efficiency.png', dpi=150, bbox_inches='tight')
plt.close()

# Chart 4: Chat Latency Distribution
fig, ax = plt.subplots(figsize=(10, 6))
models = ['3.6-flash', '3.7-flash']
p50 = [2.94, 2.11]
avg = [3.70, 3.08]
p95 = [9.28, 8.70]

x = np.arange(len(models))
width = 0.25

bars1 = ax.bar(x - width, p50, width, label='P50 (Median)', color=colors['3.6-flash'] if x[0] == 0 else colors['3.7-flash'], alpha=0.7)
bars2 = ax.bar(x, avg, width, label='Average', color=[colors['3.6-flash'], colors['3.7-flash']], alpha=0.9)
bars3 = ax.bar(x + width, p95, width, label='P95', color=[c for c in [colors['3.6-flash'], colors['3.7-flash']]], alpha=0.6)

# Recolor each group properly
for i, model in enumerate(models):
    bars1[i].set_color(colors[model])
    bars1[i].set_alpha(0.7)
    bars2[i].set_color(colors[model])
    bars2[i].set_alpha(0.9)
    bars3[i].set_color(colors[model])
    bars3[i].set_alpha(0.6)

ax.set_ylabel('Latency (seconds)', fontsize=11, fontweight='bold')
ax.set_title('Chat Response Latency from Logfire Telemetry', fontsize=13, fontweight='bold', pad=20)
ax.set_xticks(x)
ax.set_xticklabels(models)
ax.legend(fontsize=10, loc='upper left')
ax.grid(axis='y', alpha=0.3)

plt.tight_layout()
plt.savefig(results_dir / 'gemini-chat-latency.png', dpi=150, bbox_inches='tight')
plt.close()

# Chart 5: Tool Call Distribution
fig, ax = plt.subplots(figsize=(12, 6))
models = ['3.6-flash', '3.7-flash']
query_sql = [1000, 1288]
run_code = [576, 455]
aggregate = [51, 141]
other = [98, 136]

x = np.arange(len(models))
width = 0.6

p1 = ax.bar(x, query_sql, width, label='query_sql', color='#1f77b4', alpha=0.8)
p2 = ax.bar(x, run_code, width, bottom=query_sql, label='run_code', color='#ff7f0e', alpha=0.8)
p3 = ax.bar(x, aggregate, width, bottom=np.array(query_sql) + np.array(run_code), label='aggregate_events', color='#2ca02c', alpha=0.8)
p4 = ax.bar(x, other, width, bottom=np.array(query_sql) + np.array(run_code) + np.array(aggregate), label='other', color='#d62728', alpha=0.8)

ax.set_ylabel('Call Count', fontsize=11, fontweight='bold')
ax.set_title('Tool Call Distribution Across All Runs', fontsize=13, fontweight='bold', pad=20)
ax.set_xticks(x)
ax.set_xticklabels(models)
ax.legend(fontsize=10, loc='upper right')
ax.grid(axis='y', alpha=0.3)

# Add total labels
totals = [sum(v) for v in zip(query_sql, run_code, aggregate, other)]
for i, total in enumerate(totals):
    ax.text(i, total + 50, f'{total}\ntotal', ha='center', va='bottom', fontsize=9, fontweight='bold')

plt.tight_layout()
plt.savefig(results_dir / 'gemini-tool-distribution.png', dpi=150, bbox_inches='tight')
plt.close()

# Chart 6: Risk Assessment Findings
fig, ax = plt.subplots(figsize=(10, 6))
models = ['3.6-flash', '3.7-flash']
findings = [4, 3]
sql_queries = [64, 97]

x = np.arange(len(models))
width = 0.35

bars1 = ax.bar(x - width/2, findings, width, label='High-Priority Concerns', color=colors['3.6-flash'] if x[0] == 0 else colors['3.7-flash'], alpha=0.8)
ax2 = ax.twinx()
bars2 = ax2.bar(x + width/2, sql_queries, width, label='SQL Queries', color='#FFA500', alpha=0.7)

# Recolor bars properly
bars1[0].set_color(colors['3.6-flash'])
bars1[1].set_color(colors['3.7-flash'])

ax.set_xlabel('Model', fontsize=11, fontweight='bold')
ax.set_ylabel('High-Priority Concerns', fontsize=11, fontweight='bold', color='black')
ax2.set_ylabel('SQL Queries', fontsize=11, fontweight='bold', color='#FFA500')
ax.set_title('Risk Assessment: Findings vs Investigation Depth', fontsize=13, fontweight='bold', pad=20)
ax.set_xticks(x)
ax.set_xticklabels(models)
ax.grid(axis='y', alpha=0.3)

# Add value labels
for bar, val in zip(bars1, findings):
    ax.text(bar.get_x() + bar.get_width()/2., val + 0.1,
            f'{int(val)}', ha='center', va='bottom', fontsize=10, fontweight='bold')
for bar, val in zip(bars2, sql_queries):
    ax2.text(bar.get_x() + bar.get_width()/2., val + 2,
            f'{int(val)}', ha='center', va='bottom', fontsize=10, fontweight='bold', color='#FFA500')

# Create legend
lines1, labels1 = ax.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=10)

plt.tight_layout()
plt.savefig(results_dir / 'gemini-risk-assessment.png', dpi=150, bbox_inches='tight')
plt.close()

print("✅ Generated 6 charts:")
print("  - gemini-speed-performance.png")
print("  - gemini-speed-consistency.png")
print("  - gemini-query-efficiency.png")
print("  - gemini-chat-latency.png")
print("  - gemini-tool-distribution.png")
print("  - gemini-risk-assessment.png")
