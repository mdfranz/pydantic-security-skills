# Google Gemini Risk Assessment Comparison — suricata-analyst skill

**Date:** 2026-08-15
**Prompt:** "What is the overall risk posture based on this traffic? Are there high-priority concerns?"
**Skill:** `skills/suricata-analyst`
**Models:** `google:gemini-3.6-flash` vs `google:gemini-3.7-flash`
**Data:** `eve-2026-01-21-01.json` (240 MB, 428,126 events, 24h capture)
**Mode:** Pristine isolated workspaces, ambiguous/open-ended prompt

## Executive Summary

Both models tackled an open-ended security risk assessment and identified legitimate operational anomalies. However, they diverged significantly in:

1. **Risk Rating:** 3.6-flash rated as **MODERATE-TO-HIGH**; 3.7-flash as **LOW-TO-MODERATE**
2. **Investigation Depth:** 3.6-flash found 4 distinct concern categories; 3.7-flash found 3 (missing Tailscale exfiltration)
3. **SQL Query Volume:** 3.7-flash ran **97 SQL queries** vs 3.6-flash's **64** (but 3.6-flash surfaced more unique findings)
4. **Tone:** 3.6-flash more assertive on risk; 3.7-flash more conservative and operational-focused

## Findings Comparison

### Findings Both Models Identified ✓

| Finding | 3.6-flash | 3.7-flash |
|---------|-----------|-----------|
| **Host 192.168.2.180 SYN Loop** | 98,878 failed TCP attempts to `100.119.11.78:5080` | Confirmed, identified as misconfigured service |
| **SSH Transfer 523.5 MB** | Cross-VLAN data movement (VLAN 3→2) | Cross-subnet file transfer (likely backup/sync) |
| **Gateway HTTP Polling** | 10,569 flows to `192.168.2.1:8080` | Identified as aggressive polling interval (~1 req/8s) |

### Finding Only 3.6-flash Identified ⚠️

| Finding | Details |
|---------|---------|
| **High-Volume Tailscale Egress (>660 MB)** | Three hosts (`192.168.2.197`, `.173`, `.101`) uploaded >660 MB to `log.tailscale.com` and `controlplane.tailscale.com` over TLS. Identified as potential unmonitored exfiltration channel or misconfigured logging. |

This was significant enough to push 3.6-flash's risk rating from "LOW" to "MODERATE-TO-HIGH."

## Investigation Approach

### gemini-3.6-flash
- **Risk Rating:** **MODERATE-TO-HIGH OPERATIONAL & SECURITY RISK**
- **Tool Calls:** 4 (1 list_directory + 3 run_code with embedded SQL)
- **SQL Queries:** 64 (across 22 run_code calls)
- **High-Priority Findings Documented:** 4 distinct concern categories
- **Strategy:** Systematic deep dive into:
  1. Event type distribution
  2. Host-level anomalies (failed connections, large transfers)
  3. Tailscale egress patterns (cross-host analysis, data volumes)
  4. DNS/gRPC misconfiguration patterns
  5. NAT-PMP chatter
- **Key Insight:** Identified **Tailscale as a potential unmonitored egress channel** handling >660 MB, treated as exfiltration risk factor
- **Tone:** Assertive, security-focused; ranks Tailscale exfiltration as **second-highest concern**

### gemini-3.7-flash
- **Risk Rating:** **LOW-TO-MODERATE RISK**
- **Tool Calls:** 3 (1 list_directory + 2 run_code with embedded SQL)
- **SQL Queries:** 97 (embedded in fewer run_code calls, suggesting larger combined queries)
- **High-Priority Findings Documented:** 3 distinct concern categories
- **Strategy:** Focused investigation of:
  1. Alert summary (0 Suricata signature hits)
  2. Host anomalies (SYN loop, SSH transfer, gateway polling)
  3. Protocol-level assessment (DNS gRPC, TLS SNI legitimacy, QUIC benign)
- **Key Insight:** Contextualizes findings as **operational/misconfiguration** issues, not security threats; notes lack of any signature-based detections
- **Tone:** Conservative, operational-focused; treats all findings as "likely legitimate but should be verified"

## Decision Quality: Which Assessment Is More Useful?

### 3.6-flash's Strength
- Identified an additional concern (Tailscale exfiltration) that 3.7-flash overlooked
- Broke the analysis into 4 distinct risk categories (beaconing, mesh VPN egress, cross-VLAN movement, gateway polling)
- Would trigger investigation/remediation on a conservative SOC with stricter data egress policies

### 3.7-flash's Strength
- More thorough **contextual analysis** — noted that top TLS SNIs are all legitimate (Adobe, Microsoft, iCloud, etc.)
- Framed concerns as "operational/misconfiguration" rather than "potential threats," reducing false positive noise
- More cautious risk rating avoids over-alarming on what may be legitimate homelab/developer environment activity
- Mentioned the environment profile (Kubernetes clusters, Tailscale heavy, developer/homelab, typical client SaaS)

## Speed & Efficiency

| Metric | 3.6-flash | 3.7-flash |
|--------|-----------|-----------|
| **Run Code Calls** | 22 | 19 |
| **SQL Queries** | 64 | 97 |
| **Total Tool Calls** | 4 | 3 |
| **Approach** | Distributed queries across multiple run_code calls | Larger combined SQL queries in fewer calls |

3.7-flash's approach (larger, more complex SQL; fewer run_code invocations) is technically more efficient, but 3.6-flash's granular approach (more run_code calls, each targeted) may be more debuggable if a query fails.

## Risk Rating Justification

### Why 3.6-flash rated **MODERATE-TO-HIGH**
- Unknown Tailscale egress (>660 MB) = potential data exfiltration risk
- SYN loop + unreachable destination = possible C2 agent or misconfigured exfiltration tool
- Cross-VLAN SSH movement = lateral movement indicator (though likely benign)
- No signature alerts ≠ low risk (alerts may be disabled or evasion-capable)

### Why 3.7-flash rated **LOW-TO-MODERATE**
- Zero Suricata signature hits = no known exploits or malware
- All TLS SNIs are standard legitimate enterprise SaaS (Adobe, Microsoft, iCloud, etc.)
- Findings align with developer/homelab environment profile
- Anomalies are operational (failed services, aggressive polling) not malicious (no DGA, no beaconing pattern)
- Tailscale traffic *could be* legitimate overlay network, not necessarily exfiltration

## Takeaways

| Dimension | Winner | Rationale |
|-----------|--------|-----------|
| **Thoroughness** | **3.6-flash** ⚡ | Found 4 concerns vs 3; identified hidden Tailscale egress |
| **Conservatism** | 3.7-flash | Lower false-positive rate; contextualized findings as operational |
| **Security-First Approach** | 3.6-flash | More aggressive threat hunting; treats mesh VPN as risk factor |
| **Operational Context** | 3.7-flash | Better at noting legitimate SaaS and environment profile |
| **Decision Support** | Tie | 3.6-flash for "what's actually happening"; 3.7-flash for "is this a real threat?" |

## Observations on Prompt Ambiguity

This comparison reveals how ambiguous prompts expose model differences:

- **3.6-flash:** Interpreted "risk posture" as security threat assessment → deeper investigation of data flows, harder on egress patterns
- **3.7-flash:** Interpreted "risk posture" as operational health check → focused on anomaly classification and SaaS legitimacy

Both are valid, but for a security-first organization, 3.6-flash's more exhaustive (if slightly paranoid) approach may be preferable. For an operational network team, 3.7-flash's measured framing would reduce alert fatigue.

