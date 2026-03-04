"""Comprehensive analysis of pmrobot.log — March 4 run #2.

Analyses:
1. Overview stats (time range, cycles, errors)
2. Per-sport match stats  
3. Trade breakdown (PM-only vs cross-platform)
4. Cross-platform profit distribution
5. Non-moneyline leak detection (O/U, Set, Total, etc.)
6. Sanity cap rejections
7. Capital analysis (per-trade size, timing, overlap)
8. New category match results (rugby, CS2, LoL)
"""
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

LOG_PATH = "logs/pmrobot.log"

with open(LOG_PATH, encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")
ts_first = lines[0][:24]
ts_last = lines[-1][:24]
print(f"Time range: {ts_first} → {ts_last}")

def parse_ts(s):
    """Parse ISO timestamp from log line."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None

t0 = parse_ts(ts_first)
t1 = parse_ts(ts_last)
if t0 and t1:
    dur = (t1 - t0).total_seconds()
    print(f"Duration: {dur/60:.1f} minutes ({dur/3600:.2f} hours)")

# ═══════════════════════════════════════════════════════════
# 1. CROSS-SCAN CYCLES
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("1. CROSS-SCAN CYCLES")

cycle_lines = [(i, l.strip()) for i, l in enumerate(lines) if "Cross-platform scan cycle" in l]
print(f"  Total cycles: {len(cycle_lines)}")

# Group by sport
sport_cycles = defaultdict(list)
for idx, (ln, txt) in enumerate(cycle_lines):
    m = re.search(r"pm_sport=(\S+)\s+az_sport=(\S+)", txt)
    if m:
        sport_cycles[f"{m.group(1)}/{m.group(2)}"].append(txt[:24])

for sport, times in sorted(sport_cycles.items()):
    print(f"  {sport}: {len(times)} cycles")

# ═══════════════════════════════════════════════════════════
# 2. PER-SPORT MATCH STATS
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("2. PER-SPORT MATCH STATS")

alignment_lines = [l.strip() for l in lines if "Market alignment complete" in l]
# Need to associate each alignment line with its sport
# Look for the cycle line before each alignment line

sport_stats = defaultdict(lambda: {"cycles": 0, "matched": 0, "structural": 0, "llm": 0, "pm": 0, "az": 0})

# Build index: for each alignment line, find the preceding cycle line
for al in alignment_lines:
    al_ts = al[:24]
    m_pm = re.search(r"pm_count=(\d+)", al)
    m_az = re.search(r"az_count=(\d+)", al)
    m_struct = re.search(r"structural=(\d+)", al)
    m_llm = re.search(r"llm=(\d+)", al)
    m_total = re.search(r"total_matched=(\d+)", al)
    m_sport = re.search(r"sport=(\S+)", al)
    
    sport = m_sport.group(1) if m_sport else "unknown"
    pm_c = int(m_pm.group(1)) if m_pm else 0
    az_c = int(m_az.group(1)) if m_az else 0
    struct = int(m_struct.group(1)) if m_struct else 0
    llm = int(m_llm.group(1)) if m_llm else 0
    total = int(m_total.group(1)) if m_total else 0
    
    s = sport_stats[sport]
    s["cycles"] += 1
    s["matched"] += total
    s["structural"] += struct
    s["llm"] += llm
    s["pm"] += pm_c
    s["az"] += az_c

print(f"  {'Sport':<25} {'Cyc':>4} {'PM':>5} {'AZ':>5} {'Struct':>6} {'LLM':>5} {'Total':>6} {'Avg':>5}")
print(f"  {'-'*25} {'-'*4} {'-'*5} {'-'*5} {'-'*6} {'-'*5} {'-'*6} {'-'*5}")
for sport, s in sorted(sport_stats.items(), key=lambda x: -x[1]["matched"]):
    avg = s["matched"] / s["cycles"] if s["cycles"] else 0
    avg_pm = s["pm"] / s["cycles"] if s["cycles"] else 0
    avg_az = s["az"] / s["cycles"] if s["cycles"] else 0
    print(f"  {sport:<25} {s['cycles']:>4} {avg_pm:>5.0f} {avg_az:>5.0f} {s['structural']:>6} {s['llm']:>5} {s['matched']:>6} {avg:>5.1f}")

# ═══════════════════════════════════════════════════════════
# 3. ALL TRADES BREAKDOWN
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("3. TRADE BREAKDOWN")

cross_execute = []  # "Would execute cross-platform arb"
cross_simulated = []  # "Simulated cross-platform arb"
pm_arb = []  # "Would execute arbitrage"
pm_short = []  # "Would execute short arbitrage"

for line in lines:
    ls = line.strip()
    if "DRY RUN: Would execute cross-platform arb" in ls:
        cross_execute.append(ls)
    elif "DRY RUN: Simulated cross-platform arb" in ls:
        cross_simulated.append(ls)
    elif "DRY RUN: Would execute short arbitrage" in ls:
        pm_short.append(ls)
    elif "DRY RUN: Would execute arbitrage" in ls:
        pm_arb.append(ls)

print(f"  Cross-platform arb executions: {len(cross_execute)}")
print(f"  Cross-platform arb simulations: {len(cross_simulated)}")
print(f"  PM-only arbitrage: {len(pm_arb)}")
print(f"  PM-only short arbitrage (Mint+Sell): {len(pm_short)}")

# ═══════════════════════════════════════════════════════════
# 4. CROSS-PLATFORM PROFIT DISTRIBUTION
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("4. CROSS-PLATFORM PROFIT DISTRIBUTION")

profits = []
trade_details = []
for t in cross_simulated:
    m_pct = re.search(r"net_profit_pct=([\d.]+)%", t)
    m_usd = re.search(r"profit_usdc=\$([\d.]+)", t)
    m_pm = re.search(r"pm_market='([^']+)'", t)
    m_az = re.search(r"az_market='([^']+)'", t)
    m_yes = re.search(r"price_yes=([\d.]+)", t)
    m_no = re.search(r"price_no=([\d.]+)", t)
    m_cost = re.search(r"total_cost=([\d.]+)", t)
    m_strat = re.search(r"strategy=(\S+)", t)
    m_size = re.search(r"trade_size=\$([\d.]+)", t)
    
    pct = float(m_pct.group(1)) if m_pct else 0
    usd = float(m_usd.group(1)) if m_usd else 0
    pm_q = m_pm.group(1) if m_pm else ""
    az_q = m_az.group(1) if m_az else ""
    yes_p = float(m_yes.group(1)) if m_yes else 0
    no_p = float(m_no.group(1)) if m_no else 0
    cost = float(m_cost.group(1)) if m_cost else 0
    strat = m_strat.group(1) if m_strat else ""
    size = float(m_size.group(1)) if m_size else 100.0
    ts = t[:24]
    
    profits.append(pct)
    trade_details.append({
        "ts": ts, "pct": pct, "usd": usd, "pm": pm_q, "az": az_q,
        "yes": yes_p, "no": no_p, "cost": cost, "strategy": strat, "size": size
    })

if profits:
    profits_sorted = sorted(profits)
    total_usd = sum(d["usd"] for d in trade_details)
    print(f"  Total trades: {len(profits)}")
    print(f"  Total simulated P&L: ${total_usd:.2f}")
    print(f"  Min profit: {min(profits):.2f}%")
    print(f"  Max profit: {max(profits):.2f}%")
    print(f"  Mean profit: {sum(profits)/len(profits):.2f}%")
    print(f"  Median profit: {profits_sorted[len(profits)//2]:.2f}%")
    
    # Buckets
    buckets = Counter()
    for p in profits:
        if p < 1: buckets["<1%"] += 1
        elif p < 2: buckets["1-2%"] += 1
        elif p < 5: buckets["2-5%"] += 1
        elif p < 10: buckets["5-10%"] += 1
        elif p < 15: buckets["10-15%"] += 1
        elif p < 20: buckets["15-20%"] += 1
        else: buckets["≥20%"] += 1
    
    print(f"\n  Profit distribution:")
    for bucket in ["<1%", "1-2%", "2-5%", "5-10%", "10-15%", "15-20%", "≥20%"]:
        count = buckets.get(bucket, 0)
        bar = "█" * count
        print(f"    {bucket:>8}: {count:>4} {bar}")

# ═══════════════════════════════════════════════════════════
# 5. NON-MONEYLINE LEAK DETECTION
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("5. NON-MONEYLINE LEAK DETECTION")

# Check if cross-platform trades are matching AZ moneyline against PM prop bets
prop_patterns = [
    (r'\bo/u\b', "O/U (Over/Under)"),
    (r'\bover.?under\b', "Over/Under"),
    (r'\btotal\s+sets\b', "Total Sets"),
    (r'\bset\s+\d+\s+games?\b', "Set Games"),
    (r'\bmatch\s+o/u\b', "Match O/U"),
    (r'\bset\s+\d+\s+winner\b', "Set Winner"),
    (r'\b\d+(st|nd|rd|th)\s+(half|quarter|period)\s+winner\b', "Period Winner"),
    (r'\bhandicap\b', "Handicap"),
    (r'\bdraw\b', "Draw"),
    (r'\bwill\s+the\s+match\s+end\s+in\s+a\s+draw\b', "Draw Market"),
    (r'\btoss\s+winner\b', "Toss Winner"),
    (r'\btop\s+batter\b', "Top Batter"),
    (r'\bfirst\s+blood\b', "First Blood"),
]

leak_count = 0
leak_by_type = Counter()
for d in trade_details:
    pm_q = d["pm"].lower()
    for pattern, label in prop_patterns:
        if re.search(pattern, pm_q):
            leak_count += 1
            leak_by_type[label] += 1
            if leak_by_type[label] <= 2:  # Show first 2 examples
                print(f"  LEAK [{label}]: PM='{d['pm'][:80]}' ↔ AZ='{d['az'][:60]}' profit={d['pct']:.2f}%")
            break  # count once per trade

print(f"\n  Total leaks: {leak_count} / {len(trade_details)} trades")
for label, count in leak_by_type.most_common():
    print(f"    {label}: {count}")

# Trades that are NOT leaks (genuine moneyline matches)
genuine = [d for d in trade_details if not any(re.search(p, d["pm"].lower()) for p, _ in prop_patterns)]
print(f"\n  Genuine moneyline cross-platform trades: {len(genuine)}")
if genuine:
    gen_profits = [d["pct"] for d in genuine]
    gen_usd = sum(d["usd"] for d in genuine)
    print(f"    Total P&L: ${gen_usd:.2f}")
    print(f"    Profit range: {min(gen_profits):.2f}% - {max(gen_profits):.2f}%")
    print(f"    Mean: {sum(gen_profits)/len(gen_profits):.2f}%")

# ═══════════════════════════════════════════════════════════
# 6. SANITY CAP REJECTIONS
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("6. SANITY CAP REJECTIONS")

sanity_lines = [l.strip() for l in lines if "Rejecting opportunity: profit exceeds sanity cap" in l]
print(f"  Total rejections: {len(sanity_lines)}")

sanity_profits = []
for sl in sanity_lines:
    m = re.search(r"net_profit=([\d.]+)%", sl)
    if m:
        sanity_profits.append(float(m.group(1)))

if sanity_profits:
    print(f"  Rejected profit range: {min(sanity_profits):.2f}% - {max(sanity_profits):.2f}%")
    print(f"  Mean rejected profit: {sum(sanity_profits)/len(sanity_profits):.2f}%")

# ═══════════════════════════════════════════════════════════
# 7. CAPITAL ANALYSIS
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("7. CAPITAL ANALYSIS")

# Cross-platform trade details with timestamps
cross_with_ts = []
for line in lines:
    ls = line.strip()
    if "DRY RUN: Would execute cross-platform arb" in ls:
        ts = parse_ts(ls[:24])
        m_size = re.search(r"size=\$([\d.]+)", ls)
        m_profit = re.search(r"profit=([\d.]+)%", ls)
        m_pm = re.search(r"pm_q='([^']+)'", ls)
        m_az = re.search(r"az_q='([^']+)'", ls)
        m_yes = re.search(r"yes_odds=([\d.]+)", ls)
        m_no = re.search(r"no_odds=([\d.]+)", ls)
        if ts and m_size:
            cross_with_ts.append({
                "ts": ts,
                "size": float(m_size.group(1)),
                "profit_pct": float(m_profit.group(1)) if m_profit else 0,
                "pm": m_pm.group(1) if m_pm else "",
                "az": m_az.group(1) if m_az else "",
            })

# PM-only trades 
pm_with_ts = []
for line in lines:
    ls = line.strip()
    if "DRY RUN: Would execute arbitrage" in ls or "DRY RUN: Would execute short arbitrage" in ls:
        ts = parse_ts(ls[:24])
        m_size = re.search(r"trade_size=\$([\d.]+)", ls)
        m_profit = re.search(r"profit=([\d.]+)", ls)
        m_mkt = re.search(r"market=(\S+)", ls)
        if ts:
            pm_with_ts.append({
                "ts": ts,
                "size": float(m_size.group(1)) if m_size else 100.0,
                "profit_pct": float(m_profit.group(1)) * 100 if m_profit else 0,
                "market": m_mkt.group(1) if m_mkt else "",
            })

print(f"\n  a) Per-trade investment:")
print(f"     Cross-platform trade size: ${cross_with_ts[0]['size']:.2f}" if cross_with_ts else "     No cross trades")
print(f"     PM-only trade size: ${pm_with_ts[0]['size']:.2f}" if pm_with_ts else "     No PM trades")

# Trade timing clusters
if cross_with_ts:
    print(f"\n  b) Trade timing:")
    # Group trades by timestamp (same second = same scan cycle)
    from itertools import groupby
    trade_groups = []
    current_group = [cross_with_ts[0]]
    for t in cross_with_ts[1:]:
        if (t["ts"] - current_group[-1]["ts"]).total_seconds() < 5:
            current_group.append(t)
        else:
            trade_groups.append(current_group)
            current_group = [t]
    trade_groups.append(current_group)
    
    print(f"     Trade bursts (same scan cycle): {len(trade_groups)}")
    for gi, group in enumerate(trade_groups):
        ts = group[0]["ts"].strftime("%H:%M:%S")
        total_capital = sum(t["size"] for t in group)
        print(f"     Burst {gi+1}: {ts} — {len(group)} trades × ${group[0]['size']:.0f} = ${total_capital:.0f} capital needed")
    
    print(f"\n  c) Capital overlap analysis:")
    print(f"     All cross-platform trades in a burst happen simultaneously.")
    print(f"     Each trade occupies capital until the event resolves.")
    
    # Calculate max simultaneous capital
    max_burst_capital = max(sum(t["size"] for t in g) for g in trade_groups)
    max_burst_trades = max(len(g) for g in trade_groups)
    print(f"     Max single burst: {max_burst_trades} trades = ${max_burst_capital:.0f}")
    
    # Total unique events across all bursts (de-dup by AZ market)
    all_az_markets = set()
    for g in trade_groups:
        for t in g:
            all_az_markets.add(t["az"])
    print(f"     Unique AZ markets traded: {len(all_az_markets)}")
    
    # If multiple bursts, capital from earlier bursts is still locked
    if len(trade_groups) > 1:
        cumulative = 0
        for gi, group in enumerate(trade_groups):
            burst_capital = sum(t["size"] for t in group)
            cumulative += burst_capital
            ts = group[0]["ts"].strftime("%H:%M:%S")
            print(f"     After burst {gi+1} ({ts}): cumulative capital locked = ${cumulative:.0f}")
        print(f"     NOTE: Capital from earlier bursts remains locked until events resolve.")
        print(f"     Events typically resolve within hours to days.")

# ═══════════════════════════════════════════════════════════
# 8. ERRORS & WARNINGS
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("8. ERRORS & WARNINGS")

errors = [(i, l.strip()) for i, l in enumerate(lines) if "[error" in l.lower()]
print(f"  Total errors: {len(errors)}")
error_types = Counter()
for _, e in errors:
    # Extract key part
    m = re.search(r'\[error\s*\]\s*(.+?)(?:\s+\[)', e)
    if m:
        error_types[m.group(1).strip()[:60]] += 1
for etype, count in error_types.most_common(10):
    print(f"    {count:>3}x {etype}")

ws_close = [l for l in lines if "WebSocket closed" in l or "WS closed" in l or "connection closed" in l.lower()]
print(f"  WebSocket closures: {len(ws_close)}")

# ═══════════════════════════════════════════════════════════
# 9. NEW CATEGORIES RESULTS
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("9. NEW CATEGORIES (RUGBY, CS2, LOL)")

new_cats = {"rugby", "counter-strike", "league-of-legends"}
for sport in sorted(new_cats):
    s = sport_stats.get(sport, None)
    if s:
        print(f"\n  {sport}:")
        print(f"    Cycles: {s['cycles']}, PM: {s['pm']}, AZ: {s['az']}")
        print(f"    Structural: {s['structural']}, LLM: {s['llm']}, Total matched: {s['matched']}")
    else:
        print(f"\n  {sport}: Not found in alignment logs")

# Check LLM rejection reasons for new categories
for sport in new_cats:
    llm_rejections = []
    for line in lines:
        if sport in line.lower() and "LLM batch judgement" in line and "match=False" in line:
            m = re.search(r"reason='([^']+)'", line)
            if m:
                llm_rejections.append(m.group(1)[:100])
    if llm_rejections:
        print(f"\n  {sport} LLM rejection samples (first 3):")
        for r in llm_rejections[:3]:
            print(f"    - {r}")

# ═══════════════════════════════════════════════════════════
# 10. TOP TRADES (GENUINE ONLY)
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("10. TOP 10 CROSS-PLATFORM TRADES (ALL)")

for i, d in enumerate(sorted(trade_details, key=lambda x: -x["pct"])[:10]):
    print(f"  {i+1:>2}. {d['pct']:>6.2f}% (${d['usd']:.2f})  PM='{d['pm'][:60]}'")
    print(f"      AZ='{d['az'][:60]}'  yes={d['yes']:.4f} no={d['no']:.4f} cost={d['cost']:.4f}")

# Check for "Will X win?" format trades (rugby)
print(f"\n{'='*70}")
print("11. RUGBY 'WILL X WIN?' TRADES")
rugby_trades = [d for d in trade_details if "will " in d["pm"].lower() and " win" in d["pm"].lower()]
print(f"  Found: {len(rugby_trades)}")
for d in rugby_trades:
    print(f"  PM='{d['pm']}' ↔ AZ='{d['az']}' profit={d['pct']:.2f}%")

# PM-only trade details
print(f"\n{'='*70}")
print("12. PM-ONLY TRADES")
for line in lines:
    ls = line.strip()
    if "DRY RUN: Would execute arbitrage" in ls or "DRY RUN: Would execute short arbitrage" in ls:
        print(f"  {ls[:200]}")
