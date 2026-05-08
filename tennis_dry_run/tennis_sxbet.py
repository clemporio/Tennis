"""
SX Bet API client for tennis match winner markets.

Read-only — no order placement.  Handles rate limiting (0.5 s between calls)
and retries on HTTP 429.

SX Bet odds model (peer-to-peer exchange):
  - percentageOdds / 10^20  = maker's implied probability
  - As a taker you bet the OPPOSITE side to the maker:
      taker decimal odds = 1 / (1 - maker_implied_prob)
  - USDC uses 6 decimal places: raw_amount / 10^6 = USD value
"""

from __future__ import annotations

import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TennisSXBet:
    """Client for SX Bet tennis markets (read-only)."""

    BASE_URL = "https://api.sx.bet"
    TENNIS_SPORT_ID = 6
    MATCH_WINNER_TYPE = 52
    # Filter junk maker orders below this size before "best back" selection.
    # Reason: SX Bet tennis books often contain $5 makers at 13+ odds; without
    # a floor those would win the highest-decimal-odds pick and block placement.
    MIN_AVAILABLE_USD = 25.0

    # Minimum gap between HTTP requests (seconds)
    _RATE_LIMIT_SECS = 0.5
    # How many times to retry on 429 before giving up
    _MAX_RETRIES = 5
    # Backoff base (seconds) on 429 — doubles each retry
    _RETRY_BACKOFF_BASE = 2.0

    def __init__(self, min_available_usd: Optional[float] = None) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._last_call_ts: float = 0.0
        self._min_available_usd = (
            min_available_usd if min_available_usd is not None
            else self.MIN_AVAILABLE_USD
        )

    # ------------------------------------------------------------------
    # Private helpers (pure / deterministic — tested directly)
    # ------------------------------------------------------------------

    def _maker_implied_prob(self, percentage_odds: int) -> float:
        """Convert SX Bet percentageOdds to the maker's implied probability.

        Args:
            percentage_odds: raw integer value from the API

        Returns:
            Implied probability in [0, 1]
        """
        return float(percentage_odds) / 1e20

    def _taker_decimal_odds(self, percentage_odds: int) -> float:
        """Convert SX Bet percentageOdds to the taker's decimal odds.

        Because SX Bet is a P2P exchange, the taker bets the opposite side to
        the maker, so:
            taker_prob  = 1 - maker_prob
            taker_odds  = 1 / taker_prob

        Args:
            percentage_odds: raw integer value from the API

        Returns:
            Decimal odds from the taker's perspective (≥ 1.0)
        """
        maker_prob = self._maker_implied_prob(percentage_odds)
        taker_prob = 1.0 - maker_prob
        return 1.0 / taker_prob

    def _available_usd(self, total_bet_size: int, fill_amount: int) -> float:
        """Compute the available liquidity in USD from raw USDC amounts.

        USDC uses 6 decimal places on SX Bet.

        Args:
            total_bet_size: raw total order size (USDC micro-units)
            fill_amount:    raw amount already filled (USDC micro-units)

        Returns:
            Remaining order size in USD
        """
        return (int(total_bet_size) - int(fill_amount)) / 1e6

    def _normalize_market(self, raw: dict) -> Optional[dict]:
        """Normalize a raw API market dict.

        Filters to match-winner (type == 52) markets only.

        Args:
            raw: a single market dict as returned by the API

        Returns:
            Normalized dict or None if the market is not a match winner
        """
        if raw.get("type") != self.MATCH_WINNER_TYPE:
            return None

        return {
            "market_hash": raw["marketHash"],
            "player_a": raw["outcomeOneName"],
            "player_b": raw["outcomeTwoName"],
            "game_time": raw["gameTime"],
            "league": raw.get("leagueLabel", ""),
            "event_id": raw.get("sportXeventId", ""),
        }

    def _parse_orderbook(
        self, orders: list[dict], back_outcome_one: bool
    ) -> Optional[dict]:
        """Find the best available back price from an orderbook.

        In a P2P exchange the taker "takes" maker orders on the opposite side.
        To *back* outcome one, take orders where ``isMakerBettingOutcomeOne``
        is False, and vice-versa.

        Among eligible orders, return the one with the **highest** decimal
        odds (best price for the taker).  Orders with zero available liquidity
        or decimal odds ≤ 1.0 are skipped.

        Args:
            orders:           list of order dicts from the API
            back_outcome_one: True → back player A (outcome one),
                              False → back player B (outcome two)

        Returns:
            Dict with keys ``decimal_odds``, ``implied_prob``, ``available_usd``
            or None if no eligible orders exist.
        """
        # When backing outcome one we take orders where maker is on outcome TWO
        # (isMakerBettingOutcomeOne=False), and vice-versa.
        maker_side = not back_outcome_one

        best: Optional[dict] = None

        for order in orders:
            if order["isMakerBettingOutcomeOne"] != maker_side:
                continue

            available = self._available_usd(
                order["totalBetSize"], order["fillAmount"]
            )
            if available < self._min_available_usd:
                continue

            decimal_odds = self._taker_decimal_odds(order["percentageOdds"])
            if decimal_odds <= 1.0:
                continue

            if best is None or decimal_odds > best["decimal_odds"]:
                best = {
                    "decimal_odds": decimal_odds,
                    "implied_prob": 1.0 / decimal_odds,
                    "available_usd": available,
                }

        return best

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        """Make a rate-limited GET request with retry on 429.

        Args:
            path:   API path (e.g. "/leagues/active")
            params: optional query parameters

        Returns:
            Parsed JSON response body

        Raises:
            requests.HTTPError: on non-retriable HTTP errors
            RuntimeError:       if max retries on 429 is exceeded
        """
        url = self.BASE_URL + path

        # Enforce rate limit
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._RATE_LIMIT_SECS:
            time.sleep(self._RATE_LIMIT_SECS - elapsed)

        backoff = self._RETRY_BACKOFF_BASE
        for attempt in range(self._MAX_RETRIES + 1):
            self._last_call_ts = time.monotonic()
            response = self._session.get(url, params=params, timeout=30)

            if response.status_code == 429:
                if attempt == self._MAX_RETRIES:
                    raise RuntimeError(
                        f"Rate-limited after {self._MAX_RETRIES} retries: {url}"
                    )
                logger.warning(
                    "429 from %s — waiting %.1f s before retry %d/%d",
                    url,
                    backoff,
                    attempt + 1,
                    self._MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2.0
                continue

            response.raise_for_status()
            return response.json()

        # Should never reach here
        raise RuntimeError("Unexpected exit from retry loop")  # pragma: no cover

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_active_tennis_leagues(self) -> list[dict]:
        """Fetch all active leagues for tennis (sport ID 6).

        Returns:
            List of league dicts from the API filtered to tennis
        """
        data = self._request("/leagues/active")
        leagues: list[dict] = data.get("data", data) if isinstance(data, dict) else data
        return [lg for lg in leagues if lg.get("sportId") == self.TENNIS_SPORT_ID]

    def get_match_winner_markets(self, league_id: int) -> list[dict]:
        """Fetch and normalize all match-winner markets for a given league.

        Handles pagination via ``nextKey``.

        Args:
            league_id: numeric SX Bet league identifier

        Returns:
            List of normalized market dicts (type 52 only)
        """
        markets: list[dict] = []
        params: dict = {
            "leagueId": league_id,
            "typeGroup": "game-lines",
            "pageSize": 100,
        }

        while True:
            resp = self._request("/markets/active", params=params)
            if not resp or not isinstance(resp, dict):
                break
            inner = resp.get("data", {})
            if isinstance(inner, dict):
                raw_markets = inner.get("markets", [])
                next_key = inner.get("nextKey")
            else:
                raw_markets = inner if isinstance(inner, list) else []
                next_key = None

            for raw in raw_markets:
                if not isinstance(raw, dict):
                    continue
                normalized = self._normalize_market(raw)
                if normalized is not None:
                    markets.append(normalized)

            if not next_key or not raw_markets:
                break
            params = {**params, "paginationKey": next_key}

        return markets

    def get_all_tennis_markets(self) -> list[dict]:
        """Fetch match-winner markets across all active tennis leagues.

        Returns:
            Combined list of normalized market dicts
        """
        all_markets: list[dict] = []
        leagues = self.get_active_tennis_leagues()
        for league in leagues:
            league_id = league.get("leagueId") or league.get("id")
            if league_id is None:
                logger.warning("Skipping league with no ID: %s", league)
                continue
            markets = self.get_match_winner_markets(league_id)
            all_markets.extend(markets)
        return all_markets

    def get_best_back_odds(
        self,
        market_hash: str,
        player_name: str,
        market_player_a: str,
    ) -> Optional[dict]:
        """Fetch the best available back price for a player in a market.

        Args:
            market_hash:    SX Bet market hash (e.g. "0xabc123")
            player_name:    The player whose back odds we want
            market_player_a: The market's outcomeOneName (player A)

        Returns:
            Dict with ``decimal_odds``, ``implied_prob``, ``available_usd``
            or None if no liquidity is available.
        """
        data = self._request("/orders", params={"marketHashes": market_hash})
        orders: list[dict] = (
            data.get("data", []) if isinstance(data, dict) else data
        )

        # Determine which outcome we are backing
        back_outcome_one = player_name.strip() == market_player_a.strip()

        return self._parse_orderbook(orders, back_outcome_one=back_outcome_one)
