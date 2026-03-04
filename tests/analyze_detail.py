import re
lines = open(r"d:\projects\pmrobot\logs\pmrobot.log", encoding="utf-8").readlines()
dry_runs = [l for l in lines if "DRY RUN: Simulated cross-platform" in l]
hockey_kw = ['penguins','bruins','rangers','flames','devils','senators','leafs','canadiens','oilers','lightning','wild','avalanche','ducks','stars','capitals','flyers','sabres','panthers','islanders','kraken','predators','jets','kings','sharks','hurricanes','canucks','blackhawks','golden knights','blues']
hcount = sum(1 for l in dry_runs if any(k in l.lower() for k in hockey_kw))
print(f"Total DRY RUN: {len(dry_runs)}, hockey: {hcount}, tennis+other: {len(dry_runs)-hcount}")
print()
pm_qs = set()
for l in dry_runs:
    m = re.search(r"pm_market='(.+?)' net_profit", l)
    if m:
        pm_qs.add(m.group(1)[:60])
for q in sorted(pm_qs):
    sport = "HOCKEY" if any(k in q.lower() for k in hockey_kw) else "tennis"
    print(f"  [{sport:6s}] {q}")

print()
# Profit distribution
profits = []
for l in dry_runs:
    m = re.search(r"net_profit_pct=([\d.]+)%", l)
    if m:
        profits.append(float(m.group(1)))
if profits:
    buckets = {"0-5%": 0, "5-10%": 0, "10-15%": 0, "15-20%": 0}
    for p in profits:
        if p < 5:
            buckets["0-5%"] += 1
        elif p < 10:
            buckets["5-10%"] += 1
        elif p < 15:
            buckets["10-15%"] += 1
        else:
            buckets["15-20%"] += 1
    print("Profit distribution:")
    for b, c in buckets.items():
        print(f"  {b}: {c} trades")
    print(f"  Total $100 trades at avg {sum(profits)/len(profits):.2f}%: simulated profit = ${sum(p * 100 / 100 for p in profits):.2f}")
