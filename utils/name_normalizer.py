"""Team / player name normalisation utilities.

Provides a mapping of common name variants to canonical keys for
structural matching of sports events across platforms.

Example:
    >>> normalize_team_name("Manchester City")
    "man_city"
    >>> normalize_team_name("Man City")
    "man_city"
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Mapping tables – extend as needed
# ---------------------------------------------------------------------------

# Canonical key -> list of known variants (all lowercase)
_FOOTBALL_ALIASES: dict[str, list[str]] = {
    "man_city": ["manchester city", "man city", "mcfc"],
    "man_utd": ["manchester united", "man utd", "man united", "mufc"],
    "liverpool": ["liverpool", "lfc", "liverpool fc"],
    "arsenal": ["arsenal", "afc", "arsenal fc"],
    "chelsea": ["chelsea", "cfc", "chelsea fc"],
    "tottenham": ["tottenham", "tottenham hotspur", "spurs", "thfc"],
    "newcastle": ["newcastle", "newcastle united", "nufc"],
    "aston_villa": ["aston villa", "avfc"],
    "brighton": ["brighton", "brighton & hove albion", "bhafc"],
    "west_ham": ["west ham", "west ham united", "whufc"],
    "crystal_palace": ["crystal palace", "cpfc"],
    "everton": ["everton", "efc"],
    "nottm_forest": ["nottingham forest", "nottm forest", "nffc"],
    "fulham": ["fulham", "fulham fc", "ffc"],
    "bournemouth": ["bournemouth", "afc bournemouth"],
    "wolves": ["wolves", "wolverhampton", "wolverhampton wanderers"],
    "brentford": ["brentford", "brentford fc"],
    "leicester": ["leicester", "leicester city", "lcfc"],
    "southampton": ["southampton", "saints"],
    "ipswich": ["ipswich", "ipswich town"],

    # La Liga
    "barcelona": ["barcelona", "fc barcelona", "barca", "fcb"],
    "real_madrid": ["real madrid", "madrid", "rmcf"],
    "atletico": ["atletico madrid", "atletico", "atleti"],

    # Serie A
    "inter": ["inter milan", "inter", "internazionale"],
    "ac_milan": ["ac milan", "milan"],
    "juventus": ["juventus", "juve"],
    "napoli": ["napoli", "ssc napoli"],
    "roma": ["roma", "as roma"],

    # Bundesliga
    "bayern": ["bayern munich", "bayern", "fc bayern", "bayern münchen"],
    "dortmund": ["borussia dortmund", "dortmund", "bvb"],

    # Ligue 1
    "psg": ["paris saint-germain", "psg", "paris sg"],

    # Champions League / generic
    "benfica": ["benfica", "sl benfica"],
    "porto": ["porto", "fc porto"],
    "ajax": ["ajax", "afc ajax"],
}

_NBA_ALIASES: dict[str, list[str]] = {
    "lakers": ["los angeles lakers", "la lakers", "lakers"],
    "celtics": ["boston celtics", "celtics"],
    "warriors": ["golden state warriors", "warriors", "gsw"],
    "bucks": ["milwaukee bucks", "bucks"],
    "nuggets": ["denver nuggets", "nuggets"],
    "heat": ["miami heat", "heat"],
    "knicks": ["new york knicks", "knicks", "ny knicks"],
    "nets": ["brooklyn nets", "nets"],
    "76ers": ["philadelphia 76ers", "76ers", "sixers", "philly"],
    "suns": ["phoenix suns", "suns"],
    "mavericks": ["dallas mavericks", "mavericks", "mavs"],
    "clippers": ["los angeles clippers", "la clippers", "clippers"],
    "thunder": ["oklahoma city thunder", "okc thunder", "thunder"],
    "timberwolves": ["minnesota timberwolves", "timberwolves", "wolves"],
    "cavaliers": ["cleveland cavaliers", "cavaliers", "cavs"],
}


# Build reverse lookup: lowercase variant -> canonical key
_REVERSE_MAP: dict[str, str] = {}

for canonical, aliases in {**_FOOTBALL_ALIASES, **_NBA_ALIASES}.items():
    for alias in aliases:
        _REVERSE_MAP[alias.lower()] = canonical


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_team_name(name: str) -> str:
    """Normalise a team / player name to its canonical key.

    If no known alias matches, the name is lowercased, stripped of
    common suffixes (FC, SC, …), and whitespace-collapsed into
    underscores as a best-effort canonical form.

    Args:
        name: Raw team name from any platform.

    Returns:
        Canonical lowercase key (e.g. ``"man_city"``).
    """
    if not name:
        return ""

    # Try exact lookup first
    lower = name.strip().lower()
    if lower in _REVERSE_MAP:
        return _REVERSE_MAP[lower]

    # Strip common suffixes and retry
    cleaned = re.sub(r"\b(fc|sc|cf|afc|ssc|sl|as|bsc)\b", "", lower).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned in _REVERSE_MAP:
        return _REVERSE_MAP[cleaned]

    # Best-effort: lowercase + underscore
    return re.sub(r"\s+", "_", cleaned)


def teams_match(name_a: str, name_b: str) -> bool:
    """Check if two team names refer to the same team.

    Args:
        name_a: Team name from platform A.
        name_b: Team name from platform B.

    Returns:
        True if both names normalise to the same canonical key.
    """
    return normalize_team_name(name_a) == normalize_team_name(name_b)
