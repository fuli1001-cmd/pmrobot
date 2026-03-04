"""Examine genuine moneyline trades from the log."""
import re

with open(r"logs/pmrobot.log", encoding="utf-8") as f:
    lines = f.readlines()

prop_patterns = [
    r'\bo/u\b', r'\bover[/ ]under\b', r'\btotal\s+sets\b',
    r'\bset\s+\d+\s+games?\b', r'\bmatch\s+o/u\b', r'\bset\s+\d+\s+winner\b',
    r'\b\d+(st|nd|rd|th)\s+(half|quarter|period)\s+winner\b', r'\bhandicap\b',
    r'\bdraw\b', r'\btoss\s+winner\b', r'\btop\s+batter\b',
]

genuine = []
for line in lines:
    if 'Simulated cross-platform arb' not in line:
        continue
    ls = line.strip()
    m_pm = re.search(r"pm_market='([^']+)'", ls)
    m_az = re.search(r"az_market='([^']+)'", ls)
    m_pct = re.search(r'net_profit_pct=([\d.]+)%', ls)
    m_yes = re.search(r'price_yes=([\d.]+)', ls)
    m_no = re.search(r'price_no=([\d.]+)', ls)
    m_cost = re.search(r'total_cost=([\d.]+)', ls)
    m_strat = re.search(r'strategy=(\S+)', ls)
    
    if not (m_pm and m_pct):
        continue
    pm_q = m_pm.group(1)
    
    is_prop = any(re.search(p, pm_q.lower()) for p in prop_patterns)
    
    pct = float(m_pct.group(1))
    yes_p = float(m_yes.group(1)) if m_yes else 0
    no_p = float(m_no.group(1)) if m_no else 0
    cost = float(m_cost.group(1)) if m_cost else 0
    strat = m_strat.group(1) if m_strat else ""
    az_q = m_az.group(1) if m_az else ""
    
    genuine.append({
        "pct": pct, "pm": pm_q, "az": az_q,
        "yes": yes_p, "no": no_p, "cost": cost,
        "strategy": strat, "is_prop": is_prop,
    })

props = [g for g in genuine if g["is_prop"]]
moneyline = [g for g in genuine if not g["is_prop"]]

print(f"Total trades: {len(genuine)}")
print(f"Prop bet leaks: {len(props)}")
print(f"Genuine moneyline: {len(moneyline)}")

# Profile of moneyline trades
print(f"\n{'='*70}")
print("GENUINE MONEYLINE TRADES (sorted by profit desc)")
print(f"{'='*70}")

from collections import Counter

# Unique trades (deduplicate across scan cycles)
unique_pairs = {}
for g in moneyline:
    key = (g["pm"][:50], g["az"][:50])
    if key not in unique_pairs:
        unique_pairs[key] = []
    unique_pairs[key].append(g)

print(f"\nUnique PM-AZ pairs: {len(unique_pairs)}")
print(f"Total occurrences across scan cycles: {len(moneyline)}")

# Show all unique pairs
for i, ((pm_k, az_k), occurrences) in enumerate(
    sorted(unique_pairs.items(), key=lambda x: -max(o["pct"] for o in x[1]))
):
    best = max(occurrences, key=lambda o: o["pct"])
    count = len(occurrences)
    pcts = [o["pct"] for o in occurrences]
    print(f"\n{i+1:>3}. [{count}x] profit: {min(pcts):.2f}%-{max(pcts):.2f}%  cost={best['cost']:.4f}  strat={best['strategy']}")
    print(f"     PM: {best['pm'][:80]}")
    print(f"     AZ: {best['az'][:80]}")
    print(f"     yes={best['yes']:.4f}  no={best['no']:.4f}")

# Profit buckets for moneyline only
print(f"\n{'='*70}")
print("MONEYLINE PROFIT DISTRIBUTION")
buckets = Counter()
for g in moneyline:
    p = g["pct"]
    if p < 1: buckets["<1%"] += 1
    elif p < 2: buckets["1-2%"] += 1
    elif p < 5: buckets["2-5%"] += 1
    elif p < 10: buckets["5-10%"] += 1
    elif p < 15: buckets["10-15%"] += 1
    elif p < 20: buckets["15-20%"] += 1
    else: buckets["≥20%"] += 1

for bucket in ["<1%", "1-2%", "2-5%", "5-10%", "10-15%", "15-20%", "≥20%"]:
    count = buckets.get(bucket, 0)
    bar = "█" * count
    print(f"  {bucket:>8}: {count:>4} {bar}")

total_usd_ml = sum(g["pct"] for g in moneyline)  # approx (pct of $100)
print(f"\nTotal moneyline simulated P&L: ${total_usd_ml:.2f}")
print(f"Mean: {sum(g['pct'] for g in moneyline)/len(moneyline):.2f}%" if moneyline else "N/A")
