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
import unicodedata
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
    "hawks": ["atlanta hawks", "hawks"],
    "hornets": ["charlotte hornets", "hornets"],
    "bulls": ["chicago bulls", "bulls"],
    "pistons": ["detroit pistons", "pistons"],
    "pacers": ["indiana pacers", "pacers"],
    "grizzlies": ["memphis grizzlies", "grizzlies"],
    "pelicans": ["new orleans pelicans", "pelicans"],
    "magic": ["orlando magic", "magic"],
    "raptors": ["toronto raptors", "raptors"],
    "wizards": ["washington wizards", "wizards"],
    "trail_blazers": ["portland trail blazers", "trail blazers", "blazers"],
    "kings_nba": ["sacramento kings", "sacramento"],
    "spurs_nba": ["san antonio spurs", "san antonio"],
    "jazz": ["utah jazz", "jazz"],
    "rockets": ["houston rockets", "rockets"],
}

_NHL_ALIASES: dict[str, list[str]] = {
    # Atlantic Division
    "bruins": ["boston bruins", "bruins"],
    "sabres": ["buffalo sabres", "sabres"],
    "red_wings": ["detroit red wings", "red wings"],
    "panthers_nhl": ["florida panthers", "panthers"],
    "canadiens": ["montreal canadiens", "canadiens", "habs"],
    "senators": ["ottawa senators", "senators"],
    "lightning": ["tampa bay lightning", "lightning"],
    "maple_leafs": ["toronto maple leafs", "maple leafs", "leafs"],
    # Metropolitan Division
    "hurricanes": ["carolina hurricanes", "hurricanes"],
    "blue_jackets": ["columbus blue jackets", "blue jackets"],
    "devils": ["new jersey devils", "devils"],
    "islanders": ["new york islanders", "islanders"],
    "rangers": ["new york rangers", "rangers"],
    "flyers": ["philadelphia flyers", "flyers"],
    "penguins": ["pittsburgh penguins", "penguins", "pens"],
    "capitals": ["washington capitals", "capitals", "caps"],
    # Central Division
    "blackhawks": ["chicago blackhawks", "blackhawks"],
    "avalanche": ["colorado avalanche", "avalanche", "avs"],
    "stars": ["dallas stars", "stars"],
    "wild": ["minnesota wild", "wild"],
    "predators": ["nashville predators", "predators", "preds"],
    "blues": ["st. louis blues", "st louis blues", "blues"],
    "jets": ["winnipeg jets", "jets"],
    "utah_hc": ["utah hockey club", "utah"],
    # Pacific Division
    "ducks": ["anaheim ducks", "ducks"],
    "flames": ["calgary flames", "flames"],
    "oilers": ["edmonton oilers", "oilers"],
    "kings": ["los angeles kings", "la kings", "kings"],
    "sharks": ["san jose sharks", "sharks"],
    "kraken": ["seattle kraken", "kraken"],
    "canucks": ["vancouver canucks", "canucks"],
    "golden_knights": ["vegas golden knights", "golden knights", "vgk"],
}

_NFL_ALIASES: dict[str, list[str]] = {
    "cardinals": ["arizona cardinals", "cardinals"],
    "falcons": ["atlanta falcons", "falcons"],
    "ravens": ["baltimore ravens", "ravens"],
    "bills": ["buffalo bills", "bills"],
    "panthers_nfl": ["carolina panthers"],
    "bears": ["chicago bears", "bears"],
    "bengals": ["cincinnati bengals", "bengals"],
    "browns": ["cleveland browns", "browns"],
    "cowboys": ["dallas cowboys", "cowboys"],
    "broncos": ["denver broncos", "broncos"],
    "lions": ["detroit lions", "lions"],
    "packers": ["green bay packers", "packers"],
    "texans": ["houston texans", "texans"],
    "colts": ["indianapolis colts", "colts"],
    "jaguars": ["jacksonville jaguars", "jaguars", "jags"],
    "chiefs": ["kansas city chiefs", "chiefs"],
    "raiders": ["las vegas raiders", "raiders"],
    "chargers": ["los angeles chargers", "la chargers", "chargers"],
    "rams": ["los angeles rams", "la rams", "rams"],
    "dolphins": ["miami dolphins", "dolphins"],
    "vikings": ["minnesota vikings", "vikings"],
    "patriots": ["new england patriots", "patriots", "pats"],
    "saints": ["new orleans saints"],
    "giants": ["new york giants", "ny giants"],
    "jets_nfl": ["new york jets", "ny jets"],
    "eagles": ["philadelphia eagles", "eagles"],
    "steelers": ["pittsburgh steelers", "steelers"],
    "49ers": ["san francisco 49ers", "49ers", "niners"],
    "seahawks": ["seattle seahawks", "seahawks"],
    "buccaneers": ["tampa bay buccaneers", "buccaneers", "bucs"],
    "titans": ["tennessee titans", "titans"],
    "commanders": ["washington commanders", "commanders"],
}

_MLB_ALIASES: dict[str, list[str]] = {
    "diamondbacks": ["arizona diamondbacks", "diamondbacks", "d-backs"],
    "braves": ["atlanta braves", "braves"],
    "orioles": ["baltimore orioles", "orioles"],
    "red_sox": ["boston red sox", "red sox"],
    "cubs": ["chicago cubs", "cubs"],
    "white_sox": ["chicago white sox", "white sox"],
    "reds": ["cincinnati reds", "reds"],
    "guardians": ["cleveland guardians", "guardians"],
    "rockies": ["colorado rockies", "rockies"],
    "tigers": ["detroit tigers", "tigers"],
    "astros": ["houston astros", "astros"],
    "royals": ["kansas city royals", "royals"],
    "angels": ["los angeles angels", "la angels", "angels"],
    "dodgers": ["los angeles dodgers", "la dodgers", "dodgers"],
    "marlins": ["miami marlins", "marlins"],
    "brewers": ["milwaukee brewers", "brewers"],
    "twins": ["minnesota twins", "twins"],
    "mets": ["new york mets", "mets"],
    "yankees": ["new york yankees", "yankees"],
    "athletics": ["oakland athletics", "athletics", "a's"],
    "phillies": ["philadelphia phillies", "phillies"],
    "pirates": ["pittsburgh pirates", "pirates"],
    "padres": ["san diego padres", "padres"],
    "giants_mlb": ["san francisco giants"],
    "mariners": ["seattle mariners", "mariners"],
    "cardinals_mlb": ["st. louis cardinals", "st louis cardinals"],
    "rays": ["tampa bay rays", "rays"],
    "rangers_mlb": ["texas rangers"],
    "blue_jays": ["toronto blue jays", "blue jays", "jays"],
    "nationals": ["washington nationals", "nats"],
}


# Build reverse lookup: lowercase variant -> canonical key
_REVERSE_MAP: dict[str, str] = {}

for canonical, aliases in {
    **_FOOTBALL_ALIASES,
    **_NBA_ALIASES,
    **_NHL_ALIASES,
    **_NFL_ALIASES,
    **_MLB_ALIASES,
}.items():
    for alias in aliases:
        _REVERSE_MAP[alias.lower()] = canonical


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _strip_diacritics(text: str) -> str:
    """Remove accents / diacritical marks (e.g. ú → u, ñ → n)."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


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

    # Strip diacritics and retry (Criciúma → criciuma)
    ascii_lower = _strip_diacritics(lower)
    if ascii_lower != lower and ascii_lower in _REVERSE_MAP:
        return _REVERSE_MAP[ascii_lower]

    # Strip common prefixes/suffixes that vary across platforms, and retry.
    # Covers: FC, SC, CF, AFC, SSC, SL, AS, BSC, CA, CD, FK, RB, SK, IF,
    #         SV, TSG, VfL, VfB, RC, US, OGC, AJ, LOSC, Sporting, Real,
    #         Deportivo, Athletic, Atlético …
    cleaned = re.sub(
        r"\b(fc|sc|cf|afc|ssc|sl|as|bsc|ca|cd|fk|rb|sk|if|sv|tsg|vfl|vfb|"
        r"rc|us|ogc|aj|losc|ac|ss|og|se|ec|cr|cs|pk|nk|gd)\b",
        "", ascii_lower,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned in _REVERSE_MAP:
        return _REVERSE_MAP[cleaned]

    # Best-effort: lowercase, ascii, underscore-joined
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
