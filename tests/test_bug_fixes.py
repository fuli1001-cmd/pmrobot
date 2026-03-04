"""Quick validation of bug fixes."""
import sys
sys.path.insert(0, ".")

from core.alignment import MarketAligner


def test_non_moneyline_filter():
    """Test Bug 7 fix: _is_non_moneyline_question detects sub-event bets."""
    a = MarketAligner.__new__(MarketAligner)

    tests = [
        # (question, expected_is_non_moneyline)
        ("Set 1 Winner: Tabur vs Nishikori", True),
        ("1st Half Winner: Arsenal vs Chelsea", True),
        ("2nd Half Winner: Lakers vs Celtics", True),
        ("Gormaz vs Monnet: Match O/U 21.5", True),
        ("Total Sets O/U 2.5", True),
        ("Salkova vs Avanesyan: Set 1 Games O/U 8.5", True),
        ("3rd Quarter Winner", True),
        ("Set 2 Winner: Player A vs Player B", True),
        ("Handicap +3.5", True),
        ("Total points over/under", True),
        # Moneyline (should NOT be filtered)
        ("BNP Paribas Open: Lulu Sun vs Diane Parry", False),
        ("Clement Tabur vs Kei Nishikori", False),
        ("Manchester City vs Arsenal", False),
        ("Will Spain win the 2026 FIFA World Cup?", False),
        ("Antalya 2: Anhelina Kalinina vs Ekaterine Gorgodze", False),
    ]

    all_pass = True
    for question, expected in tests:
        result = a._is_non_moneyline_question(question)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_pass = False
        print(f"  {status}: \"{question[:55]}\" -> {result} (expected {expected})")

    print()
    if all_pass:
        print("All non-moneyline filter tests passed!")
    else:
        print("SOME TESTS FAILED!")
    return all_pass


def test_cross_sport_map():
    """Test Bug 3 fix: soccer and basketball removed from CROSS_SPORT_MAP."""
    from config.constants import CROSS_SPORT_MAP
    
    sports = [pm_tag for pm_tag, _ in CROSS_SPORT_MAP]
    issues = []
    if "soccer" in sports:
        issues.append("soccer still in CROSS_SPORT_MAP")
    if "basketball" in sports:
        issues.append("basketball still in CROSS_SPORT_MAP")
    if "tennis" not in sports:
        issues.append("tennis missing from CROSS_SPORT_MAP")
    if "hockey" not in sports:
        issues.append("hockey missing from CROSS_SPORT_MAP")
    
    if issues:
        print("FAIL:", "; ".join(issues))
        return False
    print("PASS: CROSS_SPORT_MAP correctly excludes soccer/basketball")
    return True


def test_sanity_cap_constant():
    """Test Bug 6 fix: _MAX_SANE_PROFIT_PCT exists."""
    from core.cross_platform import _MAX_SANE_PROFIT_PCT
    assert 0 < _MAX_SANE_PROFIT_PCT < 1.0, f"Bad cap: {_MAX_SANE_PROFIT_PCT}"
    print(f"PASS: _MAX_SANE_PROFIT_PCT = {_MAX_SANE_PROFIT_PCT:.0%}")
    return True


def test_new_categories():
    """Test new category additions: rugby, CS2, LoL + draw filter + express filter removed."""
    from config.constants import CROSS_SPORT_MAP
    issues = []

    sports = {pm_tag: az_name for pm_tag, az_name in CROSS_SPORT_MAP}

    # Rugby
    if "rugby" not in sports:
        issues.append("rugby missing from CROSS_SPORT_MAP")
    elif sports["rugby"] != "rugby":
        issues.append(f"rugby mapped to '{sports['rugby']}', expected 'rugby'")

    # Counter-Strike
    if "counter-strike" not in sports:
        issues.append("counter-strike missing from CROSS_SPORT_MAP")
    elif "Counter-Strike" not in sports["counter-strike"]:
        issues.append(f"CS mapped to '{sports['counter-strike']}', expected 'Counter-Strike'")

    # LoL
    if "league-of-legends" not in sports:
        issues.append("league-of-legends missing from CROSS_SPORT_MAP")

    # Total entries should be 10 (7 original + 3 new)
    if len(CROSS_SPORT_MAP) != 10:
        issues.append(f"CROSS_SPORT_MAP has {len(CROSS_SPORT_MAP)} entries, expected 10")

    # Draw filter in alignment
    a = MarketAligner.__new__(MarketAligner)
    if not a._is_non_moneyline_question("Will the match end in a draw?"):
        issues.append("draw question not filtered by _is_non_moneyline_question")
    if a._is_non_moneyline_question("Harlequins vs Leicester Tigers"):
        issues.append("normal match question incorrectly filtered as non-moneyline")

    # Express filter removed from Azuro GAMES_QUERY
    from exchanges.azuro import GAMES_QUERY
    if "isExpressForbidden" in GAMES_QUERY:
        issues.append("isExpressForbidden filter still in GAMES_QUERY")

    # SPORTS_TAGS includes esports
    from exchanges.polymarket import SPORTS_TAGS
    for tag in ["esports", "counter-strike", "league-of-legends"]:
        if tag not in SPORTS_TAGS:
            issues.append(f"'{tag}' missing from SPORTS_TAGS")

    if issues:
        for i in issues:
            print(f"  FAIL: {i}")
        return False
    print("PASS: New categories (rugby, CS2, LoL) correctly configured")
    print("PASS: Draw filter works")
    print("PASS: isExpressForbidden filter removed")
    print("PASS: SPORTS_TAGS includes esports tags")
    return True


if __name__ == "__main__":
    r1 = test_non_moneyline_filter()
    r2 = test_cross_sport_map()
    r3 = test_sanity_cap_constant()
    r4 = test_new_categories()
    print()
    if r1 and r2 and r3 and r4:
        print("=== ALL VALIDATIONS PASSED ===")
    else:
        print("=== SOME VALIDATIONS FAILED ===")
        sys.exit(1)
