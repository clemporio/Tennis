"""Tests for tennis_sxbet — orderbook parsing with junk-order filter.

`_parse_orderbook` picks the best taker decimal odds. To avoid the thin
junk maker orders (e.g. $5 at 13.79 odds) that have repeatedly slipped
through and blocked placement, orders below `MIN_AVAILABLE_USD` are
filtered out before the "best" selection.
"""

from unittest.mock import patch

import pytest

from tennis_sxbet import TennisSXBet


def _order(*, maker_one: bool, total_size_usd: float, percentage_odds: int) -> dict:
    """Build an order dict matching the SX Bet API shape."""
    return {
        "isMakerBettingOutcomeOne": maker_one,
        "totalBetSize": int(total_size_usd * 1e6),
        "fillAmount": 0,
        "percentageOdds": percentage_odds,
    }


# Pre-computed maker percentageOdds for various taker decimal odds.
# taker_odds = 1 / (1 - maker_prob); percentage_odds = maker_prob * 1e20.
ODDS_13_79 = 92750000000000000000   # taker 13.79
ODDS_1_45 = 31034482758620689920    # taker 1.45
ODDS_1_20 = 16666666666666665472    # taker 1.20


def test_parse_orderbook_skips_orders_below_min_available_usd():
    """Junk $5 maker @ 13.79 ignored; real $100 @ 1.45 chosen."""
    sxbet = TennisSXBet()
    orders = [
        _order(maker_one=False, total_size_usd=5.0,   percentage_odds=ODDS_13_79),
        _order(maker_one=False, total_size_usd=100.0, percentage_odds=ODDS_1_45),
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(1.45, rel=0.01)
    assert best["available_usd"] == pytest.approx(100.0, rel=0.01)


def test_parse_orderbook_returns_none_when_only_junk_below_floor():
    """If every maker order is below the floor, no liquidity is reported."""
    sxbet = TennisSXBet()
    orders = [
        _order(maker_one=False, total_size_usd=5.0,  percentage_odds=ODDS_13_79),
        _order(maker_one=False, total_size_usd=10.0, percentage_odds=ODDS_13_79),
    ]

    assert sxbet._parse_orderbook(orders, back_outcome_one=True) is None


def test_parse_orderbook_default_floor_is_25_usd():
    sxbet = TennisSXBet()
    assert sxbet.MIN_AVAILABLE_USD == 25.0
    assert sxbet._min_available_usd == 25.0


def test_parse_orderbook_respects_constructor_override():
    """Lower floor lets a thin order through (used in tests / low-stake configs)."""
    sxbet = TennisSXBet(min_available_usd=1.0)
    orders = [
        _order(maker_one=False, total_size_usd=5.0, percentage_odds=ODDS_13_79),
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(13.79, rel=0.01)


def test_parse_orderbook_when_all_orders_above_floor_picks_highest_odds():
    """Regression: with all orders above floor, highest taker decimal odds wins."""
    sxbet = TennisSXBet()
    orders = [
        _order(maker_one=False, total_size_usd=200.0, percentage_odds=ODDS_1_20),
        _order(maker_one=False, total_size_usd=100.0, percentage_odds=ODDS_1_45),
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(1.45, rel=0.01)


def test_parse_orderbook_filters_out_wrong_side():
    """Orders on the wrong maker side are ignored regardless of size."""
    sxbet = TennisSXBet()
    orders = [
        _order(maker_one=True,  total_size_usd=200.0, percentage_odds=ODDS_1_20),  # wrong
        _order(maker_one=False, total_size_usd=100.0, percentage_odds=ODDS_1_45),  # right
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(1.45, rel=0.01)


def test_get_best_back_odds_uses_plural_marketHashes_param():
    """Regression: SX Bet /orders endpoint requires `marketHashes` (plural).

    Singular `marketHash` is silently ignored — the API returns a global
    slice of unrelated orders, which the parser then mis-reads as the
    target market's book. This test pins the param name to prevent the
    bug from re-appearing.
    """
    sxbet = TennisSXBet()
    market_hash = "0xabc123"

    with patch.object(sxbet, "_request", return_value={"data": []}) as mock_req:
        sxbet.get_best_back_odds(
            market_hash=market_hash,
            player_name="Player A",
            market_player_a="Player A",
        )

    mock_req.assert_called_once()
    args, kwargs = mock_req.call_args
    params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
    assert "marketHashes" in params, (
        f"Expected 'marketHashes' (plural) per SX Bet docs; got {params!r}"
    )
    assert params["marketHashes"] == market_hash
    assert "marketHash" not in params, (
        "Singular 'marketHash' must not be sent — API ignores it"
    )
