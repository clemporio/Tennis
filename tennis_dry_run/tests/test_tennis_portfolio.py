"""Tests for tennis_portfolio — markdown render functions."""

from datetime import datetime, timezone, date

import pytest

from tennis_kelly import replay_three_bankrolls
from tennis_portfolio import render_portfolio_block


def _open(pid, ts, prob=0.8713, odds=1.4953, avail=1000.0, stake=25.0):
    return {
        "type": "open", "mode": "dry_run",
        "pick_id": pid, "pick": pid,
        "model_prob": prob, "sxbet_odds": odds,
        "sxbet_available_usd": avail, "stake": stake,
        "ts": ts,
    }


def _settled(pid, ts, won, pnl):
    return {"type": "settled", "pick_id": pid, "won": won, "pnl": pnl, "ts": ts}


def test_portfolio_block_zero_state():
    replay = replay_three_bankrolls(
        settled_trades=[], placed_trades=[], starting_balance=500.0,
        today=date(2026, 5, 8),
    )
    out = render_portfolio_block(
        replay, datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        base_stake_usd=25.0,
    )
    assert "### Portfolio (snapshot 2026-05-08 07:00 UTC)" in out
    assert "| Balance         | $500.00 | $500.00 | $500.00 |" in out
    assert "| Total P&L       | $+0.00  | $+0.00  | $+0.00  |" in out
    assert "| Today ROI       | +0.00%  | +0.00%  | +0.00%  |" in out


def test_portfolio_block_after_winning_trade():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00")]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed, today=date(2026, 5, 8))
    out = render_portfolio_block(
        replay, datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        base_stake_usd=25.0,
    )
    # Base: 500 → 512.38
    assert "| Balance         | $512.38 |" in out
    # quarter-K: 500 → 537.83 ish
    assert "$537." in out
    # half-K: 500 → 575.66 ish
    assert "$575." in out
    assert "| Today P&L       | $+12.38" in out
