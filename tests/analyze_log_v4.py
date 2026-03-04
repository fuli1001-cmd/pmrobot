"""Comprehensive analysis of pmrobot.log — March 4 run #3 (post Bug 10 fix)."""
import re
from collections import Counter, defaultdict
from datetime import datetime

LOG_PATH = "logs/pmrobot.log"

with open(LOG_PATH, encoding="utf-8") as f:
    lines = f.readlines()

def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None

ts0 = lines[0][:24]
ts1 = lines[-1][:24]
t0, t1 = parse_ts(ts0), parse_ts(ts1)
dur = (t1 - t0).total_seconds() if t0 and t1 else 0
print(f"Total lines: {len(lines)}")
print(f"Time range: {ts0} → {ts1}")
print(f"Duration: {dur/60:.1f} min ({dur/3600:.2f} hr)")

# ═════════════════════════════════════════
# 1. PER-SPORT MATCH STATS
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("1. PER-SPORT MATCH STATS")

alignment_lines = [l.strip() for l in lines if "Market alignment complete" in l]
sport_stats = defaultdict(lambda: {"cyc": 0, "matched": 0, "struct": 0, "llm": 0, "pm": 0, "az": 0})

for al in alignment_lines:
    m_sport = re.search(r"sport=(\S+)", al)
    sport = m_sport.group(1) if m_sport else "unknown"
    m_pm = re.search(r"pm_count=(\d+)", al)
    m_az = re.search(r"az_count=(\d+)", al)
    m_struct = re.search(r"structural=(\d+)", al)
    m_llm = re.search(r"llm=(\d+)", al)
    m_total = re.search(r"total_matched=(\d+)", al)
    s = sport_stats[sport]
    s["cyc"] += 1
    s["pm"] += int(m_pm.group(1)) if m_pm else 0
    s["az"] += int(m_az.group(1)) if m_az else 0
    s["struct"] += int(m_struct.group(1)) if m_struct else 0
    s["llm"] += int(m_llm.group(1)) if m_llm else 0
    s["matched"] += int(m_total.group(1)) if m_total else 0

print(f"  {'Sport':<25} {'Cyc':>3} {'AvgPM':>6} {'AvgAZ':>6} {'Struct':>6} {'LLM':>5} {'Match':>6} {'Avg':>5}")
print(f"  {'-'*25} {'-'*3} {'-'*6} {'-'*6} {'-'*6} {'-'*5} {'-'*6} {'-'*5}")
for sport, s in sorted(sport_stats.items(), key=lambda x: -x[1]["matched"]):
    c = s["cyc"] or 1
    print(f"  {sport:<25} {s['cyc']:>3} {s['pm']/c:>6.0f} {s['az']/c:>6.0f} {s['struct']:>6} {s['llm']:>5} {s['matched']:>6} {s['matched']/c:>5.1f}")

# ═════════════════════════════════════════
# 2. TRADE BREAKDOWN
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("2. TRADE BREAKDOWN")

cross_sim = [l.strip() for l in lines if "Simulated cross-platform arb" in l]
pm_arb = [l for l in lines if "DRY RUN: Would execute arbitrage" in l]
pm_short = [l for l in lines if "DRY RUN: Would execute short arbitrage" in l]

print(f"  Cross-platform simulated: {len(cross_sim)}")
print(f"  PM-only arbitrage: {len(pm_arb)}")
print(f"  PM-only short arb: {len(pm_short)}")

# ═════════════════════════════════════════
# 3. PROP BET LEAK CHECK
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("3. PROP BET LEAK CHECK (Bug 10 validation)")

prop_patterns = [
    (r'\bo/u\b', "O/U"), (r'\bover[/ ]under\b', "Over/Under"),
    (r'\btotal\s+sets\b', "Total Sets"), (r'\bset\s+\d+\s+games?\b', "Set Games"),
    (r'\bmatch\s+o/u\b', "Match O/U"), (r'\bset\s+\d+\s+winner\b', "Set Winner"),
    (r'\b\d+(st|nd|rd|th)\s+(half|quarter|period)\s+winner\b', "Period Winner"),
    (r'\bhandicap\b', "Handicap"), (r'\bdraw\b', "Draw"),
    (r'\bspread\b', "Spread"), (r'[+-]\d+\.5\b', "Spread (+/-X.5)"),
]

leak_count = 0
leak_by_type = Counter()
for t in cross_sim:
    m_pm = re.search(r"pm_market='([^']+)'", t)
    if not m_pm: continue
    pm_q = m_pm.group(1).lower()
    for pattern, label in prop_patterns:
        if re.search(pattern, pm_q):
            leak_count += 1
            leak_by_type[label] += 1
            print(f"  LEAK [{label}]: {m_pm.group(1)[:80]}")
            break

if leak_count == 0:
    print("  ✓ ZERO prop bet leaks — Bug 10 fix confirmed working!")
else:
    print(f"\n  Total leaks: {leak_count}")
    for label, count in leak_by_type.most_common():
        print(f"    {label}: {count}")

# ═════════════════════════════════════════
# 4. PROFIT DISTRIBUTION (all cross trades)
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("4. CROSS-PLATFORM PROFIT DISTRIBUTION")

trade_details = []
for t in cross_sim:
    m_pct = re.search(r"net_profit_pct=([\d.]+)%", t)
    m_usd = re.search(r"profit_usdc=\$([\d.]+)", t)
    m_pm = re.search(r"pm_market='([^']+)'", t)
    m_az = re.search(r"az_market='([^']+)'", t)
    m_yes = re.search(r"price_yes=([\d.]+)", t)
    m_no = re.search(r"price_no=([\d.]+)", t)
    m_cost = re.search(r"total_cost=([\d.]+)", t)
    m_strat = re.search(r"strategy=(\S+)", t)
    m_yon = re.search(r"yes_on=(\S+)", t)
    ts = t[:24]
    
    if not m_pct: continue
    trade_details.append({
        "ts": ts, "pct": float(m_pct.group(1)),
        "usd": float(m_usd.group(1)) if m_usd else 0,
        "pm": m_pm.group(1) if m_pm else "",
        "az": m_az.group(1) if m_az else "",
        "yes": float(m_yes.group(1)) if m_yes else 0,
        "no": float(m_no.group(1)) if m_no else 0,
        "cost": float(m_cost.group(1)) if m_cost else 0,
        "strat": m_strat.group(1) if m_strat else "",
        "yes_on": m_yon.group(1) if m_yon else "",
    })

if trade_details:
    profits = [d["pct"] for d in trade_details]
    total_usd = sum(d["usd"] for d in trade_details)
    print(f"  Total trades: {len(profits)}")
    print(f"  Total simulated P&L: ${total_usd:.2f}")
    print(f"  Min: {min(profits):.2f}%  Max: {max(profits):.2f}%  Mean: {sum(profits)/len(profits):.2f}%")
    print(f"  Median: {sorted(profits)[len(profits)//2]:.2f}%")
    
    buckets = Counter()
    for p in profits:
        if p < 1: buckets["<1%"] += 1
        elif p < 2: buckets["1-2%"] += 1
        elif p < 5: buckets["2-5%"] += 1
        elif p < 10: buckets["5-10%"] += 1
        elif p < 15: buckets["10-15%"] += 1
        elif p < 20: buckets["15-20%"] += 1
        else: buckets["≥20%"] += 1
    
    print(f"\n  Profit buckets:")
    for bucket in ["<1%", "1-2%", "2-5%", "5-10%", "10-15%", "15-20%", "≥20%"]:
        count = buckets.get(bucket, 0)
        bar = "█" * count
        print(f"    {bucket:>8}: {count:>4} {bar}")

# ═════════════════════════════════════════
# 5. UNIQUE PAIRS & DETAILS
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("5. UNIQUE MARKET PAIRS & TRADE DETAILS")

unique_pairs = defaultdict(list)
for d in trade_details:
    key = (d["pm"][:60], d["az"][:60])
    unique_pairs[key].append(d)

print(f"  Unique PM-AZ pairs: {len(unique_pairs)}")
for i, ((pm_k, az_k), occs) in enumerate(
    sorted(unique_pairs.items(), key=lambda x: -max(o["pct"] for o in x[1]))
):
    best = max(occs, key=lambda o: o["pct"])
    pcts = [o["pct"] for o in occs]
    print(f"\n  {i+1:>2}. [{len(occs)}x] {min(pcts):.2f}%-{max(pcts):.2f}%  cost={best['cost']:.4f}  yes_on={best['yes_on']}")
    print(f"      PM: {best['pm'][:75]}")
    print(f"      AZ: {best['az'][:75]}")
    print(f"      yes={best['yes']:.4f} no={best['no']:.4f}")

# ═════════════════════════════════════════
# 6. SANITY CAP REJECTIONS
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("6. SANITY CAP REJECTIONS")

sanity = [l.strip() for l in lines if "profit exceeds sanity cap" in l]
print(f"  Total: {len(sanity)}")
if sanity:
    sp = []
    for s in sanity:
        m = re.search(r"net_profit=([\d.]+)%", s)
        if m: sp.append(float(m.group(1)))
    if sp:
        print(f"  Range: {min(sp):.1f}% - {max(sp):.1f}%  Mean: {sum(sp)/len(sp):.1f}%")

# ═════════════════════════════════════════
# 7. TRADE TIMING & CAPITAL
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("7. TRADE TIMING & CAPITAL")

cross_exec = []
for line in lines:
    ls = line.strip()
    if "DRY RUN: Would execute cross-platform arb" in ls:
        ts = parse_ts(ls[:24])
        m_size = re.search(r"size=\$([\d.]+)", ls)
        m_profit = re.search(r"profit=([\d.]+)%", ls)
        if ts and m_size:
            cross_exec.append({"ts": ts, "size": float(m_size.group(1)), "pct": float(m_profit.group(1)) if m_profit else 0})

if cross_exec:
    # Group into bursts
    bursts = [[cross_exec[0]]]
    for t in cross_exec[1:]:
        if (t["ts"] - bursts[-1][-1]["ts"]).total_seconds() < 5:
            bursts[-1].append(t)
        else:
            bursts.append([t])
    
    print(f"  Trade bursts: {len(bursts)}")
    for gi, group in enumerate(bursts):
        ts = group[0]["ts"].strftime("%H:%M:%S")
        total_cap = sum(t["size"] for t in group)
        profits = [t["pct"] for t in group]
        print(f"  Burst {gi+1}: {ts}  {len(group)} trades × ${group[0]['size']:.0f} = ${total_cap:.0f}  profit range: {min(profits):.2f}%-{max(profits):.2f}%")

# ═════════════════════════════════════════
# 8. ERRORS
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("8. ERRORS & WARNINGS")

errors = [(i, l.strip()) for i, l in enumerate(lines) if "[error" in l.lower()]
print(f"  Errors: {len(errors)}")
for _, e in errors[:5]:
    m = re.search(r'\[error\s*\]\s*(.+?)(?:\s+\[)', e)
    if m: print(f"    {m.group(1)[:80]}")

ws_close = [l for l in lines if "connection closed" in l.lower() or "WebSocket closed" in l]
print(f"  WS closures: {len(ws_close)}")

# ═════════════════════════════════════════
# 9. NEW CATEGORIES
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("9. NEW CATEGORIES RESULTS")

for sport in ["rugby", "counter-strike", "league-of-legends"]:
    s = sport_stats.get(sport)
    if s:
        c = s["cyc"] or 1
        print(f"  {sport}: {s['cyc']} cyc, PM={s['pm']/c:.0f} AZ={s['az']/c:.0f}, matched={s['matched']} (struct={s['struct']} llm={s['llm']})")
    else:
        print(f"  {sport}: not found in alignment")

# Rugby/LoL trades
new_cat_trades = [d for d in trade_details if any(k in d["pm"].lower() for k in ["will ", "lol:", "counter-strike", "csgo"])]
if new_cat_trades:
    print(f"\n  New-category trades:")
    for d in new_cat_trades:
        print(f"    {d['pct']:.2f}%  PM='{d['pm'][:60]}' ↔ AZ='{d['az'][:50]}'")

# ═════════════════════════════════════════
# 10. STATS REPORT
# ═════════════════════════════════════════
print(f"\n{'='*70}")
print("10. FINAL STATS REPORT")
stats_lines = [l.strip() for l in lines if "Stats report" in l]
if stats_lines:
    last_stats = stats_lines[-1]
    print(f"  {last_stats[25:]}")
