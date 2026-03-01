"""Cross-platform market alignment.

Matches prediction-market events across Polymarket and Azuro so that
the arbitrage engine can compare prices for the *same* real-world event.

Matching strategy (in priority order):
1. **Structural rules** – normalise team names, match by
   (sport, date ± tolerance, team_a, team_b).
2. **LLM semantic fallback** – if rules fail, call an LLM to
   decide whether two questions are logically equivalent.

Results are cached by SHA-256 hash of both questions to avoid redundant
work (and LLM API calls).  The LLM cache is persisted to SQLite so
results survive restarts.
"""

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from exchanges.base import Platform, UnifiedMarket
from utils.name_normalizer import normalize_team_name, teams_match
from utils.logger import get_logger

logger = get_logger(__name__)

# Minimum LLM confidence to accept a match.
_LLM_CONFIDENCE_THRESHOLD = 0.80

# httpx timeout for LLM requests (seconds).
_LLM_TIMEOUT = 30

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
        pairs = await aligner.align(pm_markets, az_markets)
    """

    # Path for persistent SQLite cache (next to alignment.py → data/)
    _DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    _DB_PATH = os.path.join(_DB_DIR, "alignment_cache.db")

    def __init__(
        self,
        use_llm: bool = False,
        llm_api_key: str = "",
        llm_base_url: str = "https://api.openai.com/v1",
        llm_model: str = "gpt-4o-mini",
    ):
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url.rstrip("/")
        self.llm_model = llm_model
        # In-memory cache: sha256 -> AlignedMarketPair | None
        self._cache: Dict[str, Optional[AlignedMarketPair]] = {}
        # Persistent LLM-result cache (hash -> (is_match, confidence))
        self._llm_cache: Dict[str, Tuple[bool, float]] = {}
        self._init_db()

    # ------------------------------------------------------------------
    # SQLite persistent cache
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the SQLite cache table and load existing rows."""
        os.makedirs(self._DB_DIR, exist_ok=True)
        con = sqlite3.connect(self._DB_PATH)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_alignment_cache (
                hash       TEXT PRIMARY KEY,
                is_match   INTEGER NOT NULL,
                confidence REAL    NOT NULL,
                created_at REAL    NOT NULL
            )
            """
        )
        con.commit()
        # Warm in-memory cache
        rows = con.execute(
            "SELECT hash, is_match, confidence FROM llm_alignment_cache"
        ).fetchall()
        for h, m, c in rows:
            self._llm_cache[h] = (bool(m), c)
        con.close()
        if rows:
            logger.info("Loaded LLM alignment cache", entries=len(rows))

    def _persist_llm_result(
        self, cache_key: str, is_match: bool, confidence: float
    ) -> None:
        """Write an LLM result to both memory and SQLite."""
        self._llm_cache[cache_key] = (is_match, confidence)
        try:
            con = sqlite3.connect(self._DB_PATH)
            con.execute(
                """
                INSERT OR REPLACE INTO llm_alignment_cache
                    (hash, is_match, confidence, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, int(is_match), confidence, time.time()),
            )
            con.commit()
            con.close()
        except Exception as e:
            logger.warning("Failed to persist LLM cache", error=str(e))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def align(
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
            pair = await self._match_one(pm, az_markets, az_index)
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

    async def _match_one(
        self,
        pm: UnifiedMarket,
        az_markets: List[UnifiedMarket],
        az_index: Dict[str, List[UnifiedMarket]],
    ) -> Optional[AlignedMarketPair]:
        """Try to match a Polymarket market to an Azuro market.

        Priority: cache → structural rules → LLM fallback.
        """

        # ── Step 1: Check in-memory cache ──
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

        if pm_key:
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

        # ── Step 3: LLM semantic fallback ──
        if not self.use_llm or not self.llm_api_key:
            return None

        for az in az_markets:
            # Skip if time difference is too large (even LLM can't fix that)
            if not self._times_close(pm.start_time, az.start_time):
                continue

            cache_key = self._cache_key(pm.question, az.question)

            # Check persistent LLM cache first
            if cache_key in self._llm_cache:
                is_match, confidence = self._llm_cache[cache_key]
                if is_match:
                    pair = AlignedMarketPair(
                        polymarket=pm,
                        azuro=az,
                        confidence=confidence,
                        match_method="llm",
                    )
                    self._cache[cache_key] = pair
                    return pair
                # Cached negative — skip this pair
                continue

            # Call LLM
            result = await self._llm_judge(pm.question, az.question)

            if result is not None:
                is_match = result >= _LLM_CONFIDENCE_THRESHOLD
                self._persist_llm_result(cache_key, is_match, result)

                if is_match:
                    pair = AlignedMarketPair(
                        polymarket=pm,
                        azuro=az,
                        confidence=result,
                        match_method="llm",
                    )
                    self._cache[cache_key] = pair
                    logger.info(
                        "LLM match found",
                        pm_q=pm.question[:60],
                        az_q=az.question[:60],
                        confidence=f"{result:.2f}",
                    )
                    return pair

        return None

    # ------------------------------------------------------------------
    # LLM integration
    # ------------------------------------------------------------------

    async def _llm_judge(self, q1: str, q2: str) -> Optional[float]:
        """Ask the LLM whether two prediction-market questions are equivalent.

        Returns a confidence score in [0, 1], or None on failure.
        Uses the OpenAI-compatible chat-completions endpoint.
        """
        system_prompt = (
            "You are a prediction-market event matcher.  Given two questions "
            "from different platforms, determine whether they refer to the "
            "SAME real-world event and the SAME outcome direction.\n\n"
            "Respond with ONLY a JSON object: "
            '{"match": true/false, "confidence": 0.0-1.0, "reason": "..."}\n'
            "Do NOT include anything outside the JSON."
        )
        user_prompt = (
            f"Question A (Polymarket): {q1}\n"
            f"Question B (Azuro):      {q2}\n\n"
            "Are these about the same event and outcome?"
        )

        url = f"{self.llm_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
        }

        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()

            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()

            # Parse the JSON response
            # Strip markdown fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            result = json.loads(content)
            confidence = float(result.get("confidence", 0.0))
            is_match = bool(result.get("match", False))
            reason = result.get("reason", "")

            logger.debug(
                "LLM alignment judgement",
                q1=q1[:50],
                q2=q2[:50],
                match=is_match,
                confidence=f"{confidence:.2f}",
                reason=reason[:80],
            )

            return confidence if is_match else 0.0

        except httpx.HTTPStatusError as e:
            logger.warning(
                "LLM API HTTP error",
                status=e.response.status_code,
                detail=e.response.text[:200],
            )
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("LLM response parse error", error=str(e))
            return None
        except Exception as e:
            logger.warning("LLM call failed", error=str(e))
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
