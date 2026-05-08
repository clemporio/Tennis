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
