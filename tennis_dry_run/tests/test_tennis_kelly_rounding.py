"""Replay balance/total_pnl must round to journal precision on each settle."""

from __future__ import annotations

from datetime import date

import pytest

from tennis_kelly import replay_three_bankrolls


def test_replay_balance_rounds_to_two_decimals():
    """A pnl of 8.333... must accumulate as 8.33 in the replay's running balance.

    With base mode (kelly_multiplier=0), stake = base_stake = 25.0.
    Pnl on win at odds 1.3333333333 = 25.0 * 0.3333333333 = 8.3333333...
    Pre-fix: balance accumulates the unrounded value, producing 508.333...
    Post-fix: each settle rounds to 2dp, producing exactly 508.33.
    """
    placed = [{
        "type": "open", "pick_id": "p1", "ts": "2026-05-12T07:00:00+00:00",
        "model_prob": 0.85, "sxbet_odds": 1.3333333333,
        "sxbet_available_usd": 500.0, "stake": 25.0,
    }]
    settled = [{
        "type": "settled", "pick_id": "p1", "ts": "2026-05-12T15:00:00+00:00",
        "outcome": "win", "sxbet_odds": 1.3333333333, "stake": 25.0,
        "pnl": 8.33,
    }]

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 13))

    assert replay["base"]["balance"] == pytest.approx(508.33, abs=1e-9), \
        f"replay balance {replay['base']['balance']} drifted from journal sum 508.33"
    assert replay["base"]["total_pnl"] == pytest.approx(8.33, abs=1e-9), \
        f"replay total_pnl {replay['base']['total_pnl']} drifted from journal sum 8.33"
    assert round(replay["base"]["balance"] * 100) / 100.0 == replay["base"]["balance"]
    assert round(replay["base"]["total_pnl"] * 100) / 100.0 == replay["base"]["total_pnl"]
