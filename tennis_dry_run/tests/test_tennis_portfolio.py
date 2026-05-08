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


from tennis_portfolio import render_open_picks_block, render_closed_trades_block


def test_open_picks_block_empty_state():
    out = render_open_picks_block(open_picks={}, replay={
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    })
    assert "_No open picks._" in out


def test_open_picks_block_one_pick():
    open_picks = {
        "0xabc": {
            "pick_id": "0xabc", "pick": "Novak Djokovic",
            "opponent": "Dino Prizmic",
            "league": "ATP Rome", "model_prob": 0.8713,
            "sxbet_odds": 1.4953, "sxbet_available_usd": 1000.0,
            "edge": 0.2026, "stake": 25.0,
            "ts": "2026-05-08T11:55:00+00:00",
        }
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    assert "Novak Djokovic" in out
    assert "Dino Prizmic" in out
    assert "1.495" in out
    assert "+20.3" in out  # edge percentage
    # base $25, ¼K $76.40, ½K $152.81+
    assert "$25.00" in out
    assert "$76." in out
    assert "$152." in out


def test_closed_trades_block_empty():
    out = render_closed_trades_block(settled=[], placed=[], n=30)
    assert "_No closed trades yet._" in out


def test_closed_trades_block_orders_newest_first():
    placed = [
        _open("a", "2026-05-06T11:00:00+00:00", odds=1.50, stake=25.0),
        _open("b", "2026-05-08T11:00:00+00:00", odds=1.50, stake=25.0),
    ]
    settled = [
        _settled("a", "2026-05-06T15:00:00+00:00", won=True,  pnl=12.5),
        _settled("b", "2026-05-08T15:00:00+00:00", won=False, pnl=-25.0),
    ]
    out = render_closed_trades_block(settled=settled, placed=placed, n=30)
    # Newest (2026-05-08) row appears before older (2026-05-06)
    idx_b = out.index("2026-05-08")
    idx_a = out.index("2026-05-06")
    assert idx_b < idx_a
    assert "WIN" in out
    assert "LOSS" in out


def test_closed_trades_block_respects_n_limit():
    placed = [_open(f"p{i}", f"2026-05-{i+1:02d}T11:00:00+00:00") for i in range(5)]
    settled = [_settled(f"p{i}", f"2026-05-{i+1:02d}T15:00:00+00:00", won=True, pnl=12.38) for i in range(5)]
    out = render_closed_trades_block(settled=settled, placed=placed, n=2)
    # Only 2 newest dates appear in the body rows
    assert "2026-05-05" in out  # newest two: 05 and 04
    assert "2026-05-04" in out
    assert "2026-05-01" not in out


from tennis_portfolio import render_performance_block, render_backtest_comparison_block


def test_performance_block_zero_trades():
    replay = replay_three_bankrolls(settled_trades=[], placed_trades=[])
    out = render_performance_block(replay)
    assert "_No closed trades yet._" in out or "| Total trades      | 0" in out


def test_performance_block_with_one_win():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00")]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed)
    out = render_performance_block(replay)
    assert "Total trades" in out
    assert " 1 " in out  # the count
    assert "100.00%" in out  # win rate
    # avg pnl base = 12.38 / 1 = 12.38
    assert "$+12.38" in out


def test_performance_block_mixed():
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00"),
        _open("b", "2026-05-08T11:00:00+00:00"),
        _open("c", "2026-05-08T12:00:00+00:00"),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True,  pnl=12.38),
        _settled("b", "2026-05-08T16:00:00+00:00", won=False, pnl=-25.0),
        _settled("c", "2026-05-08T17:00:00+00:00", won=True,  pnl=12.38),
    ]
    replay = replay_three_bankrolls(settled, placed)
    out = render_performance_block(replay)
    assert "Total trades" in out
    assert " 3 " in out
    assert "66.67%" in out  # 2/3 win rate
    assert "2 / 1" in out   # wins / losses


def test_backtest_comparison_block():
    placed = [_open("a", "2026-05-08T10:00:00+00:00")]
    settled = [_settled("a", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed)
    out = render_backtest_comparison_block(replay)
    assert "Backtest" in out
    assert "87.4%" in out  # backtest win rate
    assert "11,161" in out  # backtest sample size
    assert "100.00%" in out  # actual base win rate
