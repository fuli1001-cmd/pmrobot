"""Detailed analysis of March 4 log - focus on new categories, DRY RUN details, and cycle structure."""
import re
from collections import Counter, defaultdict

LOG_PATH = "logs/pmrobot.log"

with open(LOG_PATH, encoding="utf-8") as f:
    lines = f.readlines()

# ── 1. Find scan cycle structure via "Azuro markets fetched" and "Polymarket markets fetched" ──
print("=" * 60)
print("1. SCAN CYCLE STRUCTURE (via market fetch logs)")

# Find all Azuro fetches with sport
az_fetches = []
for i, l in enumerate(lines):
    if "Azuro markets fetched" in l:
        m_sport = re.search(r"sport=(?:')?(\S+?)(?:'|\s|$)", l)
        m_parsed = re.search(r"parsed=(\d+)", l)
        m_raw = re.search(r"raw_games=(\d+)", l)
        sport = m_sport.group(1).rstrip("'") if m_sport else "?"
        parsed = int(m_parsed.group(1)) if m_parsed else 0
        raw = int(m_raw.group(1)) if m_raw else 0
        ts = l[:24]
        az_fetches.append({"ts": ts, "sport": sport, "parsed": parsed, "raw": raw, "line": i})

# Find all PM fetches
pm_fetches = []
for i, l in enumerate(lines):
    if "Polymarket markets fetched" in l:
        m_sport = re.search(r"sport=(\S+)", l)
        m_total = re.search(r"(?:filtered|total)=(\d+)", l)
        sport = m_sport.group(1) if m_sport else "?"
        total = int(m_total.group(1)) if m_total else 0
        ts = l[:24]
        pm_fetches.append({"ts": ts, "sport": sport, "total": total, "line": i})

# Count by sport
az_by_sport = defaultdict(list)
pm_by_sport = defaultdict(list)
for f in az_fetches:
    az_by_sport[f["sport"]].append(f)
for f in pm_fetches:
    pm_by_sport[f["sport"]].append(f)

print(f"\nAzuro fetches by sport:")
for sport, fetches in sorted(az_by_sport.items()):
    avg_parsed = sum(f["parsed"] for f in fetches) / len(fetches)
    avg_raw = sum(f["raw"] for f in fetches) / len(fetches)
    print(f"  {sport:<25} {len(fetches):>2} fetches  avg_raw={avg_raw:>5.0f}  avg_parsed={avg_parsed:>5.0f}")

print(f"\nPM fetches by sport:")
for sport, fetches in sorted(pm_by_sport.items()):
    avg_total = sum(f["total"] for f in fetches) / len(fetches)
    print(f"  {sport:<25} {len(fetches):>2} fetches  avg_total={avg_total:>5.0f}")

# ── 2. Match alignment per sport ──
print(f"\n{'='*60}")
print("2. ALIGNMENT PER SPORT")

# Find alignment lines and the preceding scan context
for i, l in enumerate(lines):
    if "Market alignment complete" in l:
        # Look back for scan context
        sport = "?"
        for j in range(max(0, i-30), i):
            if "Azuro markets fetched" in lines[j]:
                m = re.search(r"sport=(?:')?(\S+?)(?:'|\s|$)", lines[j])
                if m:
                    sport = m.group(1).rstrip("'")

alignment_by_sport = defaultdict(lambda: {"cycles": 0, "matched": [], "struct": [], "llm": []})
for i, l in enumerate(lines):
    if "Market alignment complete" not in l:
        continue
    sport = "?"
    for j in range(max(0, i-30), i):
        if "Azuro markets fetched" in lines[j]:
            m = re.search(r"sport=(?:')?(\S+?)(?:'|\s|$)", lines[j])
            if m:
                sport = m.group(1).rstrip("'")
    m_total = re.search(r"total_matched=(\d+)", l)
    m_struct = re.search(r"structural=(\d+)", l)
    m_llm = re.search(r"llm=(\d+)", l)
    total = int(m_total.group(1)) if m_total else 0
    struct = int(m_struct.group(1)) if m_struct else 0
    llm_v = int(m_llm.group(1)) if m_llm else 0
    s = alignment_by_sport[sport]
    s["cycles"] += 1
    s["matched"].append(total)
    s["struct"].append(struct)
    s["llm"].append(llm_v)

for sport, s in sorted(alignment_by_sport.items(), key=lambda x: -sum(x[1]["matched"])):
    avg = sum(s["matched"]) / len(s["matched"]) if s["matched"] else 0
    total_s = sum(s["struct"])
    total_l = sum(s["llm"])
    print(f"  {sport:<25} cycles={s['cycles']:>2}  avg_matched={avg:>5.1f}  total_struct={total_s:>4}  total_llm={total_l:>4}")

# ── 3. DRY RUN trade details ──
print(f"\n{'='*60}")
print("3. DRY RUN TRADE DETAILS")

dry_runs = []
for i, l in enumerate(lines):
    if "DRY RUN: Simulated cross-platform arb" in l:
        # Parse details
        m_pm = re.search(r"pm_market='([^']*)'", l)
        m_az = re.search(r"az_market='([^']*)'", l)
        m_profit = re.search(r"net_profit=([\d.]+)%", l)
        m_side = re.search(r"pm_side=(\w+)", l)
        
        pm_q = m_pm.group(1) if m_pm else ""
        az_q = m_az.group(1) if m_az else ""
        profit = float(m_profit.group(1)) if m_profit else 0
        side = m_side.group(1) if m_side else ""
        
        dry_runs.append({"pm": pm_q, "az": az_q, "profit": profit, "side": side, "line": i, "ts": l[:24]})

print(f"Total DRY RUN trades: {len(dry_runs)}")

if dry_runs:
    profits = [d["profit"] for d in dry_runs if d["profit"] > 0]
    if profits:
        print(f"  Avg profit: {sum(profits)/len(profits):.2f}%")
        print(f"  Min: {min(profits):.2f}%   Max: {max(profits):.2f}%")
        print(f"  Distribution:")
        for lo, hi in [(0,5),(5,10),(10,15),(15,20)]:
            cnt = sum(1 for p in profits if lo <= p < hi)
            print(f"    {lo}-{hi}%: {cnt}")

    # Identify sport from market names
    print(f"\n  Sample trades:")
    for d in dry_runs[:10]:
        print(f"    {d['ts'][:19]}  profit={d['profit']:>5.1f}%  PM='{d['pm'][:50]}'  AZ='{d['az'][:50]}'")
    
    print(f"\n  Last 5 trades:")
    for d in dry_runs[-5:]:
        print(f"    {d['ts'][:19]}  profit={d['profit']:>5.1f}%  PM='{d['pm'][:50]}'  AZ='{d['az'][:50]}'")

# ── 4. New category DRY RUNs specifically ──
print(f"\n{'='*60}")
print("4. NEW CATEGORY TRADES")

# Check if any DRY RUN involves rugby, CS, LoL keywords
for cat, keywords in [("rugby", ["rugby", "super rugby", "nrl", "top 14"]),
                      ("CS2", ["counter-strike", "cs2", "csgo"]),
                      ("LoL", ["league of legends", "lol ", "lpl", "americas cup"])]:
    cat_trades = []
    for d in dry_runs:
        combined = (d["pm"] + " " + d["az"]).lower()
        if any(kw in combined for kw in keywords):
            cat_trades.append(d)
    print(f"\n  {cat}: {len(cat_trades)} trades")
    for t in cat_trades[:5]:
        print(f"    profit={t['profit']:>5.1f}%  PM='{t['pm'][:55]}'")

# ── 5. Sanity cap rejections detail ──
print(f"\n{'='*60}")
print("5. SANITY CAP REJECTIONS")

sanity = [l for l in lines if "sanity cap" in l.lower()]
print(f"  Total: {len(sanity)}")
if sanity:
    profits = []
    for s in sanity:
        m = re.search(r"net_profit_pct=([\d.]+)", s)
        if m:
            profits.append(float(m.group(1)))
    if profits:
        print(f"  Range: {min(profits)*100:.1f}% – {max(profits)*100:.1f}%")
        print(f"  Avg: {sum(profits)/len(profits)*100:.1f}%")

# ── 6. Cumulative P&L timeline ──
print(f"\n{'='*60}")
print("6. CUMULATIVE P&L TIMELINE")

pnl_lines = [(l[:24], l) for l in lines if "simulated_profit=$" in l]
if pnl_lines:
    # Show milestones
    prev = 0
    for ts, l in pnl_lines:
        m = re.search(r"simulated_profit=\$([\d.]+)", l)
        if m:
            val = float(m.group(1))
            if val - prev > 200 or l == pnl_lines[-1][1]:
                print(f"  {ts}  ${val:>8.2f}")
                prev = val
    final = re.search(r"simulated_profit=\$([\d.]+)", pnl_lines[-1][1])
    if final:
        print(f"\n  Final simulated P&L: ${float(final.group(1)):,.2f}")

# ── 7. WebSocket reconnection stats ──
print(f"\n{'='*60}")
print("7. WEBSOCKET STATS")

ws_close = [l for l in lines if "WebSocket connection closed" in l or "WebSocket reconnect" in l.lower()]
ws_recon = [l for l in lines if "reconnect" in l.lower() and "websocket" in l.lower()]
print(f"  WS closures: {len(ws_close)}")
print(f"  WS reconnects: {len(ws_recon)}")

print(f"\n{'='*60}")
print("DONE")
