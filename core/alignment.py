"""Cross-platform market alignment.

Matches prediction-market events across Polymarket and Azuro so that
the arbitrage engine can compare prices for the *same* real-world event.

Matching strategy (in priority order):
1. **Structural rules** – normalise team names, match by
   (sport, date ± tolerance, team_a, team_b).
2. *(Future)* **LLM semantic fallback** – if rules fail, call an LLM to
   decide whether two questions are logically equivalent.

Results are cached by SHA-256 hash of both questions to avoid redundant
work (and LLM API calls).
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from exchanges.base import Platform, UnifiedMarket
from utils.name_normalizer import normalize_team_name, teams_match
from utils.logger import get_logger

logger = get_logger(__name__)

# Maximum gap (seconds) between event start times for a structural match.
_TIME_TOLERANCE_SECONDS = 6 * 3600  # 6 hours


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AlignedMarketPair:
    """A matched pair of markets across two platforms."""

    polymarket: UnifiedMarket
    azuro: UnifiedMarket
    confidence: float = 1.0  # 1.0 for structural, <1.0 for LLM
    match_method: str = "structural"
    matched_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Aligner
# ---------------------------------------------------------------------------

class MarketAligner:
    """Aligns Polymarket and Azuro markets for cross-platform comparison.

    Usage::

        aligner = MarketAligner()
        pairs = aligner.align(pm_markets, az_markets)
    """

    def __init__(self, use_llm: bool = False, llm_api_key: str = ""):
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key
        # Cache: sha256(pm_question + az_question) -> AlignedMarketPair | None
        self._cache: Dict[str, Optional[AlignedMarketPair]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def align(
        self,
        pm_markets: List[UnifiedMarket],
        az_markets: List[UnifiedMarket],
    ) -> List[AlignedMarketPair]:
        """Find matching market pairs across the two platforms.

        Args:
            pm_markets: Polymarket markets (sports-filtered).
            az_markets: Azuro markets.

        Returns:
            List of aligned pairs.
        """
        pairs: List[AlignedMarketPair] = []

        # Build lookup index for Azuro markets by normalised team pair
        az_index = self._build_team_index(az_markets)

        for pm in pm_markets:
            pair = self._match_one(pm, az_markets, az_index)
            if pair:
                pairs.append(pair)

        logger.info(
            "Market alignment complete",
            pm_count=len(pm_markets),
            az_count=len(az_markets),
            matched=len(pairs),
        )
        return pairs

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_team_index(
        self, markets: List[UnifiedMarket]
    ) -> Dict[str, List[UnifiedMarket]]:
        """Build an index: (norm_team_a, norm_team_b) -> [market, …].

        The key is a sorted tuple of normalised team names so that
        "A vs B" and "B vs A" map to the same bucket.
        """
        index: Dict[str, List[UnifiedMarket]] = {}
        for m in markets:
            key = self._team_pair_key(m.team_a, m.team_b)
            if key:
                index.setdefault(key, []).append(m)
        return index

    @staticmethod
    def _team_pair_key(team_a: str, team_b: str) -> Optional[str]:
        """Canonical key for a pair of teams (order-insensitive)."""
        na = normalize_team_name(team_a)
        nb = normalize_team_name(team_b)
        if not na or not nb:
            return None
        parts = sorted([na, nb])
        return f"{parts[0]}|{parts[1]}"

    def _match_one(
        self,
        pm: UnifiedMarket,
        az_markets: List[UnifiedMarket],
        az_index: Dict[str, List[UnifiedMarket]],
    ) -> Optional[AlignedMarketPair]:
        """Try to structurally match a Polymarket market to an Azuro market."""

        # ── Step 1: Check cache ──
        for az in az_markets:
            cache_key = self._cache_key(pm.question, az.question)
            if cache_key in self._cache:
                return self._cache[cache_key]

        # ── Step 2: Structural match via team-pair index ──
        pm_key = self._team_pair_key(pm.team_a, pm.team_b)

        # If Polymarket doesn't have structured team names, try to
        # extract them from the question text
        if not pm_key:
            pm_key = self._extract_team_pair_from_question(pm.question)

        if not pm_key:
            return None

        candidates = az_index.get(pm_key, [])
        for az in candidates:
            # Check time proximity
            if self._times_close(pm.start_time, az.start_time):
                pair = AlignedMarketPair(
                    polymarket=pm,
                    azuro=az,
                    confidence=1.0,
                    match_method="structural",
                )
                self._cache[self._cache_key(pm.question, az.question)] = pair
                logger.debug(
                    "Structural match found",
                    pm_q=pm.question[:60],
                    az_q=az.question[:60],
                )
                return pair

        return None

    def _extract_team_pair_from_question(self, question: str) -> Optional[str]:
        """Best-effort extraction of team names from a question string.

        Looks for patterns like "Will X beat Y?" or "X vs Y" or "X to win
        against Y".
        """
        import re

        q = question.lower()

        # Pattern: "X vs Y"
        m = re.search(r"(.+?)\s+vs\.?\s+(.+)", q)
        if m:
            return self._team_pair_key(m.group(1).strip(), m.group(2).strip())

        # Pattern: "Will X beat Y"
        m = re.search(r"will\s+(.+?)\s+beat\s+(.+?)[\?]?$", q)
        if m:
            return self._team_pair_key(m.group(1).strip(), m.group(2).strip())

        # Pattern: "X to win against Y"
        m = re.search(r"(.+?)\s+to\s+win\s+(?:against|vs)\s+(.+?)[\?]?$", q)
        if m:
            return self._team_pair_key(m.group(1).strip(), m.group(2).strip())

        return None

    @staticmethod
    def _times_close(t1: float, t2: float) -> bool:
        """Check if two timestamps are within the tolerance window."""
        if t1 == 0.0 or t2 == 0.0:
            # If either platform lacks start_time, allow match (less strict)
            return True
        return abs(t1 - t2) <= _TIME_TOLERANCE_SECONDS

    @staticmethod
    def _cache_key(q1: str, q2: str) -> str:
        """Deterministic cache key for a pair of questions."""
        combined = f"{q1.strip().lower()}||{q2.strip().lower()}"
        return hashlib.sha256(combined.encode()).hexdigest()
