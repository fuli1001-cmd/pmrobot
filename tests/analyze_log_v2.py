"""Analyze pmrobot.log (March 4 run) — post bug-fix + new-categories version."""
import re
import sys
from collections import Counter, defaultdict

LOG_PATH = "logs/pmrobot.log"

with open(LOG_PATH, encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")
print(f"Time range: {lines[0][:24]} → {lines[-1][:24]}")

# ── 1. Cross-scan cycles ──
cycle_starts = []
for i, line in enumerate(lines):
    if "Cross-platform scan cycle" in line:
        cycle_starts.append((i, line.strip()))
print(f"\n{'='*60}")
print(f"1. CROSS-SCAN CYCLES: {len(cycle_starts)}")
for idx, (ln, txt) in enumerate(cycle_starts):
    ts = txt[:24]
    # extract sport
    m = re.search(r"pm_sport=(\S+)\s+az_sport=(\S+)", txt)
    pm_sp = m.group(1) if m else "?"
    az_sp = m.group(2) if m else "?"
    print(f"  Cycle {idx+1:>2}: {ts}  PM={pm_sp}  AZ={az_sp}")

# ── 2. Per-sport match stats ──
print(f"\n{'='*60}")
print("2. PER-SPORT MATCH STATS")

alignment_lines = [l for l in lines if "Market alignment complete" in l]
sport_stats = defaultdict(lambda: {"cycles": 0, "total_matched": 0, "structural": 0, "llm": 0, "pm_count": 0, "az_count": 0})

for al in alignment_lines:
    # Find preceding cycle start to get sport
    ts = al[:24]
    m_pm = re.search(r"pm_count=(\d+)", al)
    m_az = re.search(r"az_count=(\d+)", al)
    m_struct = re.search(r"structural=(\d+)", al)
    m_llm = re.search(r"llm=(\d+)", al)
    m_total = re.search(r"total_matched=(\d+)", al)
    
    pm_c = int(m_pm.group(1)) if m_pm else 0
    az_c = int(m_az.group(1)) if m_az else 0
    struct = int(m_struct.group(1)) if m_struct else 0
    llm = int(m_llm.group(1)) if m_llm else 0
    total = int(m_total.group(1)) if m_total else 0

    # Infer sport from nearby "Azuro markets fetched" lines
    # Look back up to 20 lines for sport info
    line_idx = lines.index(al)
    sport = "?"
    for j in range(max(0, line_idx - 30), line_idx):
        if "Cross-platform scan cycle" in lines[j]:
            ms = re.search(r"pm_sport=(\S+)", lines[j])
            if ms:
                sport = ms.group(1)
    
    s = sport_stats[sport]
    s["cycles"] += 1
    s["total_matched"] += total
    s["structural"] += struct
    s["llm"] += llm
    s["pm_count"] += pm_c
    s["az_count"] += az_c

for sport, s in sorted(sport_stats.items(), key=lambda x: -x[1]["total_matched"]):
    avg_match = s["total_matched"] / s["cycles"] if s["cycles"] else 0
    avg_pm = s["pm_count"] / s["cycles"] if s["cycles"] else 0
    avg_az = s["az_count"] / s["cycles"] if s["cycles"] else 0
    print(f"  {sport:<22} cycles={s['cycles']:>2}  avg_match={avg_match:>5.1f}  "
          f"(struct={s['structural']:>3} llm={s['llm']:>3})  "
          f"avg_PM={avg_pm:>6.0f}  avg_AZ={avg_az:>5.0f}")

# ── 3. Opportunities & sanity cap ──
print(f"\n{'='*60}")
print("3. OPPORTUNITIES & SANITY CAP")

opp_lines = [l for l in lines if "Cross-platform opportunities found" in l]
sanity_lines = [l for l in lines if "rejected by sanity cap" in l.lower() or "Sanity cap" in l]
print(f"  Opportunity reports: {len(opp_lines)}")

opp_counts = []
best_profits = []
for ol in opp_lines:
    m_count = re.search(r"count=(\d+)", ol)
    m_best = re.search(r"best_profit=([\d.]+)%", ol)
    if m_count:
        opp_counts.append(int(m_count.group(1)))
    if m_best:
        best_profits.append(float(m_best.group(1)))

if opp_counts:
    print(f"  Total opportunities: {sum(opp_counts)}")
    print(f"  Avg per report: {sum(opp_counts)/len(opp_counts):.1f}")
if best_profits:
    print(f"  Best profit range: {min(best_profits):.2f}% – {max(best_profits):.2f}%")

print(f"  Sanity cap rejections: {len(sanity_lines)}")
if sanity_lines:
    profits = []
    for sl in sanity_lines:
        m = re.search(r"net_profit_pct=([\d.]+)", sl)
        if m:
            profits.append(float(m.group(1)))
    if profits:
        print(f"  Sanity cap profit range: {min(profits)*100:.1f}% – {max(profits)*100:.1f}%")

# ── 4. DRY RUN trades ──
print(f"\n{'='*60}")
print("4. DRY RUN TRADES")

dry_runs = [l for l in lines if "DRY RUN" in l and "cross-platform" in l.lower()]
print(f"  Total DRY RUN lines: {len(dry_runs)}")

# Parse profit and sport info
trade_profits = []
trade_sports = Counter()
trade_markets = Counter()
for dr in dry_runs:
    m_profit = re.search(r"profit(?:_pct)?=([\d.]+)%?", dr)
    if m_profit:
        p = float(m_profit.group(1))
        if p > 1:
            p = p / 100  # Normalize to fraction if given as pct
        trade_profits.append(p)
    m_sp = re.search(r"sport=(\S+)", dr)
    if m_sp:
        trade_sports[m_sp.group(1)] += 1
    m_mkt = re.search(r"pm_question='([^']*)'", dr)
    if not m_mkt:
        m_mkt = re.search(r"pm_market=(\S+)", dr)
    if m_mkt:
        trade_markets[m_mkt.group(1)[:60]] += 1

if trade_profits:
    # Determine if pct or fraction
    avg_p = sum(trade_profits) / len(trade_profits)
    if avg_p < 1:
        print(f"  Avg profit: {avg_p*100:.2f}%")
        print(f"  Profit distribution:")
        buckets = [(0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20)]
        for lo, hi in buckets:
            cnt = sum(1 for p in trade_profits if lo <= p < hi)
            print(f"    {lo*100:.0f}-{hi*100:.0f}%: {cnt}")
    else:
        print(f"  Avg profit: {avg_p:.2f}%")

print(f"\n  By sport:")
for sp, cnt in trade_sports.most_common():
    print(f"    {sp}: {cnt}")

print(f"\n  Top markets (by trade count):")
for mkt, cnt in trade_markets.most_common(10):
    print(f"    {cnt:>3}x {mkt}")

# ── 5. New categories (rugby, CS2, LoL) check ──
print(f"\n{'='*60}")
print("5. NEW CATEGORIES CHECK")

for cat in ["rugby", "counter-strike", "league-of-legends", "Counter-Strike", "League of Legends"]:
    matches = [l for l in lines if cat.lower() in l.lower()]
    if matches:
        print(f"  '{cat}': {len(matches)} log lines")
        for m in matches[:3]:
            print(f"    {m.strip()[:120]}")
    else:
        print(f"  '{cat}': 0 log lines")

# ── 6. Errors & warnings ──
print(f"\n{'='*60}")
print("6. ERRORS & WARNINGS")

errors = [l for l in lines if "[error" in l.lower()]
warnings = [l for l in lines if "[warning" in l.lower()]
print(f"  Errors: {len(errors)}")
print(f"  Warnings: {len(warnings)}")

# Show unique error patterns
error_patterns = Counter()
for e in errors:
    # Simplify: take first 80 chars after the log level
    m = re.search(r'\[error\s*\]\s*(.*)', e, re.IGNORECASE)
    if m:
        msg = m.group(1)[:80].strip()
        error_patterns[msg] += 1

if error_patterns:
    print(f"\n  Unique error patterns:")
    for pat, cnt in error_patterns.most_common(10):
        print(f"    {cnt:>3}x {pat}")

warn_patterns = Counter()
for w in warnings:
    m = re.search(r'\[warning\s*\]\s*(.*)', w, re.IGNORECASE)
    if m:
        msg = m.group(1)[:80].strip()
        warn_patterns[msg] += 1

if warn_patterns:
    print(f"\n  Unique warning patterns:")
    for pat, cnt in warn_patterns.most_common(10):
        print(f"    {cnt:>3}x {pat}")

# ── 7. Simulated P&L ──
print(f"\n{'='*60}")
print("7. SIMULATED P&L")

pnl_lines = [l for l in lines if "simulated" in l.lower() and "profit" in l.lower()]
for pl in pnl_lines[-5:]:
    print(f"  {pl.strip()[:120]}")

# Also look for total_simulated
total_sim = [l for l in lines if "total_simulated" in l.lower() or "cumulative" in l.lower()]
for tl in total_sim[-5:]:
    print(f"  {tl.strip()[:120]}")

# ── 8. Set Winner / non-moneyline leak check ──
print(f"\n{'='*60}")
print("8. NON-MONEYLINE LEAK CHECK")
set_winner = [l for l in lines if "set" in l.lower() and "winner" in l.lower() and "DRY RUN" in l]
draw_leak = [l for l in lines if "draw" in l.lower() and "DRY RUN" in l]
print(f"  'Set Winner' in DRY RUN: {len(set_winner)}")
print(f"  'Draw' in DRY RUN: {len(draw_leak)}")
if set_winner:
    for sw in set_winner[:3]:
        print(f"    {sw.strip()[:120]}")
if draw_leak:
    for dl in draw_leak[:3]:
        print(f"    {dl.strip()[:120]}")

print(f"\n{'='*60}")
print("DONE")
