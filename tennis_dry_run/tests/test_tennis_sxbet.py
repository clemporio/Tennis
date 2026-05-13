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
    """Junk $5 maker @ 13.79 ignored; real $100 @ 1.45 chosen.
    available_usd is taker stake: 100/(1.45-1) = $222.22."""
    sxbet = TennisSXBet()
    orders = [
        _order(maker_one=False, total_size_usd=5.0,   percentage_odds=ODDS_13_79),
        _order(maker_one=False, total_size_usd=100.0, percentage_odds=ODDS_1_45),
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(1.45, rel=0.01)
    assert best["available_usd"] == pytest.approx(222.22, abs=0.5)


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
    """Lower floor lets a thin order through. $50 maker @ 13.79 is taker
    stake $50/12.79 = $3.91 — passes a $1 floor but would fail $25 default."""
    sxbet = TennisSXBet(min_available_usd=1.0)
    orders = [
        _order(maker_one=False, total_size_usd=50.0, percentage_odds=ODDS_13_79),
    ]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(13.79, rel=0.01)
    assert best["available_usd"] == pytest.approx(3.91, abs=0.05)


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


def test_available_usd_returns_taker_stake_not_maker_stake():
    """SX Bet `totalBetSize` is the maker's lay stake. The taker (us) can
    only stake `maker_remaining / (decimal_odds - 1)` because that's what
    the maker can pay out at decimal_odds D.

    Live verification (2026-05-09 Humbert vs Prizmic): SX Bet UI showed
    $24 fillable at 3.32 against a $57.15 maker order — i.e. 57.15/(3.32-1)
    = 24.63. Reporting maker stake inflates underdog liquidity 2-3× and lets
    the MIN_AVAILABLE_USD floor pass orders that won't fill our base stake.
    """
    sxbet = TennisSXBet(min_available_usd=1.0)
    # Maker $57.15 at percentageOdds → taker decimal odds 3.32; expect $24.63
    PCT_3_32 = int((1 - 1/3.32) * 1e20)  # 6.987e19
    orders = [_order(maker_one=False, total_size_usd=57.15, percentage_odds=PCT_3_32)]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["decimal_odds"] == pytest.approx(3.32, abs=0.01)
    assert best["available_usd"] == pytest.approx(24.63, abs=0.05)


def test_min_available_usd_floor_applies_to_taker_stake():
    """An underdog order with $30 MAKER stake at decimal 3.32 only allows
    $30/(3.32-1) ≈ $12.93 TAKER stake. With MIN_AVAILABLE_USD=$25 it MUST
    be rejected — we'd never actually fill our base stake against it."""
    sxbet = TennisSXBet()  # default floor $25
    PCT_3_32 = int((1 - 1/3.32) * 1e20)
    orders = [_order(maker_one=False, total_size_usd=30.0, percentage_odds=PCT_3_32)]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is None, "Maker $30 @ 3.32 = taker $12.93, must fail $25 floor"


def test_min_available_usd_floor_passes_when_taker_stake_above_floor():
    """A favorite order at decimal 1.40 with $26 maker stake gives taker
    $26/0.40 = $65 — comfortably above the $25 floor."""
    sxbet = TennisSXBet()
    PCT_1_40 = int((1 - 1/1.40) * 1e20)
    orders = [_order(maker_one=False, total_size_usd=26.0, percentage_odds=PCT_1_40)]

    best = sxbet._parse_orderbook(orders, back_outcome_one=True)

    assert best is not None
    assert best["available_usd"] == pytest.approx(65.0, abs=0.5)


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
