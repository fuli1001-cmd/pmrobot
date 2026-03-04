"""Analyze the latest pmrobot.log run."""
import re
from collections import Counter, defaultdict

lines = open(r"d:\projects\pmrobot\logs\pmrobot.log", encoding="utf-8").readlines()

# 1. Basic stats
first_ts = lines[0][:23] if lines else "?"
last_ts = lines[-1][:23] if lines else "?"
print(f"Log: {len(lines)} lines, {first_ts} -> {last_ts}")
print()

# 2. Cross-scan cycles
cycles = [l for l in lines if "Cross-scan cycle complete" in l]
print(f"Cross-scan cycles: {len(cycles)}")
for l in cycles[:3]:
    print(f"  {l.strip()[:160]}")
print(f"  ... (showing first 3 of {len(cycles)})")
print()

# 3. Per-sport breakdown from "Cross-scan sport matched"
sport_data = defaultdict(list)
for l in lines:
    if "Cross-scan sport matched" not in l:
        continue
    m = re.search(r"az=(\d+)\s+pairs=(\d+)\s+pm=(\d+)\s+sport=(\S+)", l)
    if m:
        sport_data[m.group(4)].append({
            "az": int(m.group(1)),
            "pairs": int(m.group(2)),
            "pm": int(m.group(3)),
        })

print("Per-sport matching (avg across cycles):")
for sport, entries in sorted(sport_data.items(), key=lambda x: -sum(e["pairs"] for e in x[1])):
    avg_pairs = sum(e["pairs"] for e in entries) / len(entries)
    avg_pm = sum(e["pm"] for e in entries) / len(entries)
    avg_az = sum(e["az"] for e in entries) / len(entries)
    print(f"  {sport:12s}: {len(entries):2d} cycles, avg {avg_pairs:.0f} pairs (PM~{avg_pm:.0f} x AZ~{avg_az:.0f})")
print()

# 4. Opportunities per cycle
opp_lines = [l for l in lines if "Cross-platform opportunities found" in l]
profits = []
counts = []
for l in opp_lines:
    m_profit = re.search(r"best_profit=([\d.]+)%", l)
    m_count = re.search(r"count=(\d+)", l)
    if m_profit:
        profits.append(float(m_profit.group(1)))
    if m_count:
        counts.append(int(m_count.group(1)))

print(f"Opportunities: {len(opp_lines)} cycles with opportunities")
if profits:
    print(f"  best_profit range: {min(profits):.2f}% - {max(profits):.2f}%, avg {sum(profits)/len(profits):.2f}%")
if counts:
    print(f"  count range: {min(counts)} - {max(counts)}, avg {sum(counts)/len(counts):.1f}")
print()

# 5. Sanity cap rejections
rejects = [l for l in lines if "Rejecting opportunity" in l]
print(f"Sanity cap rejections (>20% profit): {len(rejects)}")
if rejects:
    reject_profits = []
    for l in rejects:
        m = re.search(r"net_profit=([\d.]+)%", l)
        if m:
            reject_profits.append(float(m.group(1)))
    if reject_profits:
        print(f"  rejected profit range: {min(reject_profits):.1f}% - {max(reject_profits):.1f}%")
print()

# 6. DRY RUN executions
dry_runs = [l for l in lines if "DRY RUN: Would execute cross-platform" in l]
simulated = [l for l in lines if "DRY RUN: Simulated cross-platform" in l]
print(f"DRY RUN executions: {len(dry_runs)}")
print(f"Simulated trades: {len(simulated)}")

# Profit from simulated trades
sim_profits = []
for l in simulated:
    m = re.search(r"net_profit_pct=([\d.]+)%", l)
    if m:
        sim_profits.append(float(m.group(1)))
if sim_profits:
    print(f"  simulated profit range: {min(sim_profits):.2f}% - {max(sim_profits):.2f}%")
    print(f"  avg profit per trade: {sum(sim_profits)/len(sim_profits):.2f}%")
print()

# 7. Stats report
stats_lines = [l for l in lines if "Stats report" in l]
if stats_lines:
    print("Latest stats report:")
    print(f"  {stats_lines[-1].strip()[:200]}")
print()

# 8. Set Winner bug check
set_winner_dry = [l for l in lines if "Set" in l and "Winner" in l and "DRY RUN" in l]
set_winner_opp = [l for l in lines if "Set" in l and "Winner" in l and "opportunity" in l.lower()]
print(f"Set Winner in DRY RUN (should be 0): {len(set_winner_dry)}")
print(f"Set Winner in opportunities (should be 0): {len(set_winner_opp)}")
print()

# 9. Unique DRY RUN opportunities (by PM question)
pm_questions = Counter()
for l in dry_runs:
    m = re.search(r"pm_q='([^']+)'", l)
    if m:
        pm_questions[m.group(1)] += 1

print(f"Unique PM markets traded: {len(pm_questions)}")
print("Top traded PM markets:")
for q, c in pm_questions.most_common(10):
    print(f"  [{c:2d}x] {q[:80]}")
print()

# 10. Per-sport opportunity breakdown  
sport_opps = Counter()
for l in dry_runs:
    q = ""
    m = re.search(r"pm_q='([^']+)'", l)
    if m:
        q = m.group(1).lower()
    # Infer sport from question patterns
    if any(x in q for x in ["vs.", "open", "antalya", "kigali", "paris", "dubai", "lyon",
                             "thionville", "hersonissos", "atp", "wta", "bnp"]):
        sport_opps["tennis"] += 1
    elif any(x in q for x in ["penguins", "bruins", "rangers", "flames", "devils",
                               "senators", "leafs", "canadiens", "oilers", "hawks"]):
        sport_opps["hockey"] += 1
    else:
        sport_opps["other"] += 1

print("DRY RUN by sport (heuristic):")
for s, c in sport_opps.most_common():
    print(f"  {s}: {c}")
