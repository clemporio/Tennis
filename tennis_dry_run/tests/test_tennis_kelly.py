"""Tests for tennis_kelly — Kelly math and three-bankroll replay."""

import pytest

from tennis_kelly import kelly_fraction


def test_kelly_fraction_djokovic_example():
    # p=0.8713, odds=1.4953  →  f* = (0.8713*1.4953 - 1) / (1.4953 - 1)
    #                            = (1.30279 - 1) / 0.4953  ≈  0.61146
    assert kelly_fraction(prob=0.8713, decimal_odds=1.4953) == pytest.approx(0.61146, abs=0.0001)


def test_kelly_fraction_negative_edge_clamps_to_zero():
    # market 1.50 implies 66.7%; model says 50%  →  f* would be negative
    assert kelly_fraction(prob=0.50, decimal_odds=1.50) == 0.0


def test_kelly_fraction_zero_when_break_even():
    # p×odds == 1 exactly  →  f* = 0
    assert kelly_fraction(prob=0.5, decimal_odds=2.0) == 0.0


def test_kelly_fraction_clamps_at_one_for_max_input():
    # p=1.0 (sure win) gives f* = 1.0; never exceeds it
    assert kelly_fraction(prob=1.0, decimal_odds=2.0) == 1.0
    assert kelly_fraction(prob=1.0, decimal_odds=1.5) == 1.0


from tennis_kelly import day_start_stake


def test_day_start_stake_base_mode_returns_flat_25():
    out = day_start_stake(
        mode="base",
        base_stake=25.0,
        kelly_multiplier=0.0,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out == {"stake": 25.0, "pre_cap_stake": 25.0, "capped": False}


def test_day_start_stake_quarter_kelly_uses_balance():
    # Djokovic: f* = 0.6112; 0.25 * f* * 500 = $76.40
    out = day_start_stake(
        mode="quarter_kelly",
        base_stake=25.0,
        kelly_multiplier=0.25,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out["stake"] == pytest.approx(76.40, abs=0.05)
    assert out["pre_cap_stake"] == pytest.approx(76.40, abs=0.05)
    assert out["capped"] is False


def test_day_start_stake_half_kelly_uses_balance():
    # 0.5 * 0.61146 * 500 = $152.87
    out = day_start_stake(
        mode="half_kelly",
        base_stake=25.0,
        kelly_multiplier=0.5,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out["stake"] == pytest.approx(152.87, abs=0.05)
    assert out["capped"] is False


def test_day_start_stake_caps_to_liquidity():
    # half-Kelly wants $152.87 but book only has $39
    out = day_start_stake(
        mode="half_kelly",
        base_stake=25.0,
        kelly_multiplier=0.5,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=39.0,
    )
    assert out["stake"] == 39.0
    assert out["pre_cap_stake"] == pytest.approx(152.87, abs=0.05)
    assert out["capped"] is True


def test_day_start_stake_base_caps_to_liquidity_too():
    # Base $25 but book only has $10 (rare but possible)
    out = day_start_stake(
        mode="base",
        base_stake=25.0,
        kelly_multiplier=0.0,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=10.0,
    )
    assert out == {"stake": 10.0, "pre_cap_stake": 25.0, "capped": True}


def test_day_start_stake_kelly_zero_on_negative_edge():
    # Kelly stake should be 0 when edge is negative
    out = day_start_stake(
        mode="quarter_kelly",
        base_stake=25.0,
        kelly_multiplier=0.25,
        day_start_balance=500.0,
        prob=0.50, decimal_odds=1.50,   # negative edge
        liquidity_usd=1000.0,
    )
    assert out == {"stake": 0.0, "pre_cap_stake": 0.0, "capped": False}


from datetime import datetime, timezone
from tennis_kelly import replay_three_bankrolls


def _open(pick_id, ts, prob, odds, avail, stake=25.0):
    return {
        "type": "open", "mode": "dry_run",
        "pick_id": pick_id, "pick": pick_id,
        "model_prob": prob, "sxbet_odds": odds,
        "sxbet_available_usd": avail, "stake": stake,
        "ts": ts,
    }


def _settled(pick_id, ts, won: bool, base_pnl: float):
    return {
        "type": "settled", "pick_id": pick_id,
        "outcome": "win" if won else "loss",
        "pnl": base_pnl,
        "ts": ts,
    }


def test_replay_empty_returns_starting_balance():
    out = replay_three_bankrolls(settled_trades=[], placed_trades=[], starting_balance=500.0)
    for mode in ("base", "quarter_kelly", "half_kelly"):
        assert out[mode]["balance"] == 500.0
        assert out[mode]["peak_balance"] == 500.0
        assert out[mode]["drawdown_pct"] == 0.0
        assert out[mode]["total_pnl"] == 0.0
        assert out[mode]["wins"] == 0
        assert out[mode]["losses"] == 0
        assert out[mode]["capped_count"] == 0


def test_replay_single_winning_trade():
    # Djokovic example: $25 base, $76.40 quarter-K, $152.81 half-K
    # Win at 1.4953 odds → profit = stake * (1.4953 - 1) = stake * 0.4953
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.38)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(512.38, abs=0.01)
    assert out["base"]["wins"] == 1
    assert out["quarter_kelly"]["balance"] == pytest.approx(537.83, abs=0.05)
    assert out["half_kelly"]["balance"] == pytest.approx(575.66, abs=0.06)


def test_replay_single_losing_trade():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=False, base_pnl=-25.0)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(475.0, abs=0.01)
    assert out["base"]["losses"] == 1
    assert out["quarter_kelly"]["balance"] == pytest.approx(423.60, abs=0.05)
    assert out["half_kelly"]["balance"] == pytest.approx(347.19, abs=0.06)


def test_replay_drawdown_resets_on_new_peak():
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", "2026-05-09T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("c", "2026-05-10T10:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True,  base_pnl=12.5),   # 500 → 512.50
        _settled("b", "2026-05-09T15:00:00+00:00", won=False, base_pnl=-25.0),  # 512.50 → 487.50, drawdown
        _settled("c", "2026-05-10T15:00:00+00:00", won=True,  base_pnl=12.5),   # 487.50 → 500.00, still drawdown vs 512.50
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["peak_balance"] == pytest.approx(512.50, abs=0.01)
    assert out["base"]["balance"] == pytest.approx(500.00, abs=0.01)
    # drawdown = (peak - bal) / peak = 12.5 / 512.5 = 2.439%
    assert out["base"]["drawdown_pct"] == pytest.approx(2.439, abs=0.01)


def test_replay_day_start_balance_carries_over():
    # Day 1: base balance 500 → 512.5 after win. Day 2 start balance = 512.5.
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", "2026-05-09T10:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.5),
        _settled("b", "2026-05-09T15:00:00+00:00", won=True, base_pnl=12.5),
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(525.0, abs=0.01)
    # quarter-K day 2 stake uses 512.5 + 12.5_quarter_pnl_day_1
    # f*(0.80, 1.50) = (1.20-1)/0.50 = 0.40
    # day 1 q-K stake = 0.25 * 0.40 * 500 = 50; pnl = 50 * 0.50 = +25
    # day 2 q-K start = 525; stake = 0.25 * 0.40 * 525 = 52.50; pnl = 52.50 * 0.50 = +26.25
    # final = 525 + 26.25 = 551.25
    assert out["quarter_kelly"]["balance"] == pytest.approx(551.25, abs=0.05)


def test_replay_capped_trade_uses_liquidity_for_stake():
    # half-K wants 152.81 but liquidity is 39.05 → capped
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 39.05)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.38)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    # half-Kelly stake capped at 39.05; pnl = 39.05 * 0.4953 = 19.34
    assert out["half_kelly"]["balance"] == pytest.approx(519.34, abs=0.05)
    assert out["half_kelly"]["capped_count"] == 1


def test_replay_today_pnl_and_today_roi():
    # Two trades on day 1 (today UTC), one win one loss
    today = datetime.now(timezone.utc).date().isoformat()
    placed = [
        _open("a", f"{today}T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", f"{today}T11:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", f"{today}T15:00:00+00:00", won=True,  base_pnl=12.5),
        _settled("b", f"{today}T16:00:00+00:00", won=False, base_pnl=-25.0),
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["today_pnl"] == pytest.approx(-12.5, abs=0.01)
    # day_start_balance for today = 500 (no prior days)
    # today_roi = -12.5 / 500 = -2.5%
    assert out["base"]["today_roi_pct"] == pytest.approx(-2.5, abs=0.01)


def test_replay_deployed_sums_open_stake_per_mode():
    # One placed-but-not-yet-settled trade
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    out = replay_three_bankrolls(settled_trades=[], placed_trades=placed, starting_balance=500.0)
    assert out["base"]["deployed"] == 25.0
    assert out["quarter_kelly"]["deployed"] == pytest.approx(76.40, abs=0.05)
    assert out["half_kelly"]["deployed"] == pytest.approx(152.81, abs=0.06)
