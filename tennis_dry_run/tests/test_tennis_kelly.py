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
