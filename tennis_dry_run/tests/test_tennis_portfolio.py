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
    return {
        "type": "settled", "pick_id": pid,
        "outcome": "win" if won else "loss",
        "pnl": pnl, "ts": ts,
    }


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


from tennis_portfolio import render_identified_picks_block


def test_identified_picks_block_empty():
    out = render_identified_picks_block(selections=[])
    assert "_No qualifying selections today._" in out


def test_identified_picks_block_renders_model_only_columns():
    """Identifier doesn't fetch the orderbook (late-binding to placer at T-15),
    so the BOD report must not advertise scan-time SX Bet odds / edge / liquidity
    columns it can never populate. Keep model+match metadata only."""
    selections = [
        {
            "pick": "Iga Swiatek", "opponent": "Catherine McNally",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.8848, "fair_odds": 1.130,
            "game_time_iso": "2026-05-08T09:00:00+00:00",
            "placement_path": "scheduled", "scheduled_at_iso": "2026-05-08T08:45:00+00:00",
        },
        {
            "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.8713, "fair_odds": 1.148,
            "game_time_iso": "2026-05-08T12:10:00+00:00",
            "placement_path": "immediate", "scheduled_at_iso": None,
        },
    ]
    out = render_identified_picks_block(selections)
    assert "Iga Swiatek" in out
    assert "Catherine McNally" in out
    assert "Novak Djokovic" in out
    assert "1.130" in out
    assert "1.148" in out
    assert "scheduled 08:45" in out
    assert "immediate" in out
    assert "0.8848" in out
    # Removed (always-empty) columns must not appear in the header.
    assert "SX Bet @07:00" not in out
    assert "| Edge |" not in out
    assert "Liquidity" not in out


from tennis_portfolio import (
    render_today_placer_activity_block,
    render_today_settlements_block,
)


def test_today_placer_activity_empty():
    out = render_today_placer_activity_block(placed_today=[], placer_skips=[], replay={
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    })
    assert "_No placer activity today._" in out


def test_today_placer_activity_mixed():
    placed = [{
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "model_prob": 0.8713, "sxbet_odds": 1.4953,
        "sxbet_available_usd": 1000.0, "edge": 0.2026,
        "stake": 25.0, "ts": "2026-05-08T11:55:00+00:00",
    }]
    skipped = [{
        "pick": "Alexander Zverev", "opponent": "Daniel Altmaier",
        "league": "ATP Rome", "sxbet_odds": 1.087,
        "edge": -0.063, "reason": "negative_edge",
        "ts": "2026-05-08T10:45:00+00:00",
    }]
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_today_placer_activity_block(placed_today=placed, placer_skips=skipped, replay=replay)
    assert "Djokovic" in out
    assert "placed" in out
    assert "Zverev" in out
    assert "skipped: negative_edge" in out
    assert "$25.00" in out  # Djokovic base
    assert "$76." in out    # Djokovic ¼K
    assert "$152." in out   # Djokovic ½K
    assert "—" in out       # Zverev stake columns


def test_today_settlements_empty():
    out = render_today_settlements_block(settlements=[], placed_lookup={})
    assert "_No settlements today._" in out


def test_open_picks_block_flags_pick_without_sxbet_odds():
    """Legacy state.json entries without sxbet_odds render as flagged
    `_(incomplete data)_` rows — not silently dropped. The header count
    excludes them so it reflects fully-renderable picks only."""
    open_picks = {
        "0xlegacy": {
            "pick_id": "0xlegacy", "pick": "Legacy Pick",
            "opponent": "Mystery", "league": "?",
            # no sxbet_odds key — written by old version of bot
            "model_prob": 0.85, "edge": 0.10,
            "ts": "2026-05-01T10:00:00+00:00",
        }
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    # Header count = 0 (no fully-renderable picks)
    assert "### Open Picks (0)" in out
    # Incomplete pick is surfaced, not silently lost
    assert "Legacy Pick" in out
    assert "(incomplete data)" in out


def test_open_picks_block_uses_game_time_for_match_column():
    """When `game_time` (unix epoch seconds) is on the pick, the Match (UTC)
    column should render it — not the placement timestamp."""
    # game_time = 1778590800 → 2026-05-12 13:00:00 UTC (Zverev v Darderi).
    open_picks = {
        "0xzv": {
            "pick_id": "0xzv", "pick": "Alexander Zverev",
            "opponent": "Luciano Darderi", "league": "ATP Rome",
            "model_prob": 0.8495, "sxbet_odds": 1.2085,
            "sxbet_available_usd": 263.87, "edge": 0.022,
            "stake": 25.0,
            "ts": "2026-05-11T07:00:40+00:00",  # placement time — must NOT appear
            "game_time": 1778590800,
        }
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    assert "Alexander Zverev" in out
    # Match column = actual match time (UTC)
    assert "2026-05-12 13:00" in out
    # Placement timestamp must NOT appear in the Match column anymore
    assert "2026-05-11 07:00" not in out


def test_open_picks_block_renders_dash_when_game_time_missing():
    """Legacy picks without game_time get an honest em-dash in the Match column —
    NOT the placement timestamp (which would be misleading)."""
    open_picks = {
        "0xlegacy": {
            "pick_id": "0xlegacy", "pick": "Old Pick",
            "opponent": "Someone", "league": "ATP Rome",
            "model_prob": 0.8, "sxbet_odds": 1.5,
            "sxbet_available_usd": 100.0, "edge": 0.05,
            "stake": 25.0,
            "ts": "2026-05-11T07:00:40+00:00",
            # no game_time key
        }
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    assert "Old Pick" in out
    assert "2026-05-11 07:00" not in out  # placement time MUST NOT appear


# ── render_stale_carryover_block ──────────────────────────────────────────────

from tennis_portfolio import render_stale_carryover_block


def _ts_unix(year, month, day, hour, minute=0):
    from datetime import datetime, timezone
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def test_stale_carryover_block_empty_when_no_unsettled_today():
    """All today's picks settled → block renders the empty placeholder."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [{
        "pick_id": "0xdjk", "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 12, 10),
    }]
    settled_today = [{"pick_id": "0xdjk", "ts": "2026-05-08T15:00:00+00:00"}]
    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=[], settled_today=settled_today, now_utc=now,
    )
    assert "_No stale carryovers." in out or "_No carryover picks today." in out


def test_stale_carryover_block_lists_picks_skipped_at_placement():
    """A pick whose game_time was today, never settled, with placer skip event,
    should appear with the skip reason and last seen sxbet_odds."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [
        {"pick_id": "0xzv", "pick": "Alexander Zverev", "opponent": "Daniel Altmaier",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 11, 0)},
        {"pick_id": "0xsw", "pick": "Iga Swiatek", "opponent": "Catherine McNally",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 8, 9, 0)},
    ]
    placer_skips = [
        {"pick_id": "0xzv", "pick": "Alexander Zverev", "reason": "negative_edge",
         "sxbet_odds": 1.087, "edge": -0.063, "ts": "2026-05-08T10:45:00+00:00",
         "source": "placer"},
        {"pick_id": "0xsw", "pick": "Iga Swiatek", "reason": "odds_out_of_range_at_placement",
         "sxbet_odds": 16.0, "ts": "2026-05-08T08:45:00+00:00", "source": "placer"},
    ]

    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=placer_skips,
        settled_today=[], now_utc=now,
    )

    assert "## Stale Carryovers" in out
    assert "Alexander Zverev" in out
    assert "Daniel Altmaier" in out
    assert "negative_edge" in out
    assert "1.087" in out
    assert "Iga Swiatek" in out
    assert "odds_out_of_range_at_placement" in out
    assert "16.000" in out


def test_stale_carryover_block_marks_picks_with_no_placer_attempt():
    """If a pick's game_time was today but no placer skip event exists and no
    settled trade, mark it as 'no placer attempt logged' — surfaces lost picks."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [{
        "pick_id": "0xlost", "pick": "Mystery Player", "opponent": "Other",
        "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 8, 14, 0),
    }]

    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=[],
        settled_today=[], now_utc=now,
    )

    assert "Mystery Player" in out
    assert "no placer attempt" in out


def test_stale_carryover_block_excludes_future_matches():
    """Picks whose game_time is still in the future (placer hasn't fired yet)
    are NOT carryovers — they're still pending. Exclude them."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [
        {"pick_id": "0xpast", "pick": "Past Match", "opponent": "X",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 11, 0)},
        {"pick_id": "0xfut", "pick": "Future Match", "opponent": "Y",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 23, 30)},
    ]

    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=[],
        settled_today=[], now_utc=now,
    )

    assert "Past Match" in out
    assert "Future Match" not in out


def test_stale_carryover_block_excludes_picks_from_other_days():
    """Picks whose game_time was on a previous UTC day shouldn't appear in
    today's EOD section. (They'll have been pruned by the next morning's run.)"""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [
        {"pick_id": "0xy", "pick": "Yesterday Pick", "opponent": "X",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 7, 14, 0)},
        {"pick_id": "0xt", "pick": "Today Pick", "opponent": "Y",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 8, 14, 0)},
    ]

    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=[],
        settled_today=[], now_utc=now,
    )

    assert "Today Pick" in out
    assert "Yesterday Pick" not in out


def test_stale_carryover_block_dedups_pending_by_pick_id_keeping_latest():
    """If pending file has multiple entries for the same pick_id (re-identifier
    runs), only one row appears in the carryover block."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    pending = [
        {"pick_id": "0xdup", "pick": "Same Player", "opponent": "X",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 11, 0)},
        {"pick_id": "0xdup", "pick": "Same Player", "opponent": "X",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 11, 0)},
    ]
    out = render_stale_carryover_block(
        pending=pending, placer_skips_today=[],
        settled_today=[], now_utc=now,
    )
    assert out.count("Same Player") == 1


# ── render_shadow_picks_block ─────────────────────────────────────────────────

from tennis_portfolio import render_shadow_picks_block


def test_shadow_picks_block_empty():
    """No tier-B selections today → empty placeholder."""
    out = render_shadow_picks_block(selections=[])
    assert "_No shadow (tier B) picks today._" in out


def test_shadow_picks_block_renders_tier_B_only():
    """Renders model + match metadata for shadow picks. No placement column
    since shadow picks never get placed."""
    selections = [
        {
            "pick": "Player A", "opponent": "Player B",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.74, "fair_odds": 1.351,
            "tier": "B",
            "game_time_iso": "2026-05-09T14:00:00+00:00",
        },
        {
            "pick": "Player C", "opponent": "Player D",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.78, "fair_odds": 1.282,
            "tier": "B",
            "game_time_iso": "2026-05-09T16:00:00+00:00",
        },
    ]
    out = render_shadow_picks_block(selections)
    assert "## Shadow Picks (tier B, 70-80% — not placed)" in out
    assert "Player A" in out
    assert "Player C" in out
    assert "1.351" in out
    assert "1.282" in out
    assert "0.7400" in out
    # Must not have a Placement column — tier B never gets placed.
    assert "Placement" not in out


def test_shadow_picks_block_renders_T90_placement_columns():
    """When selections include `shadow_placement` (the T-90 evaluation done
    by tennis_shadow_placer), the renderer surfaces a 'T-90 result' column
    showing would_place / skip-reason and the SX Bet odds at that fire moment."""
    selections = [
        {
            "pick": "Ben Shelton", "opponent": "Basilashvili",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
            "game_time_iso": "2026-05-10T10:10:00+00:00",
            "shadow_placement": {
                "status": "would_place", "sxbet_odds": 3.31,
                "available_usd": 284.5, "edge": 0.3733,
                "ts": "2026-05-10T08:40:00+00:00",
            },
        },
        {
            "pick": "Other Pick", "opponent": "Foe",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
            "game_time_iso": "2026-05-10T12:00:00+00:00",
            "shadow_placement": {
                "status": "would_skip", "reason": "negative_edge",
                "sxbet_odds": 1.20, "edge": -0.0933,
                "ts": "2026-05-10T10:30:00+00:00",
            },
        },
    ]
    out = render_shadow_picks_block(selections)
    assert "T-90" in out  # column header
    assert "would_place" in out
    assert "negative_edge" in out
    assert "3.310" in out  # sxbet_odds at T-90 for Ben Shelton
    assert "1.200" in out  # sxbet_odds at T-90 for Other Pick


def test_shadow_picks_block_with_outcomes_renders_status_and_pnl():
    """When selections include `status` + `theoretical_pnl` (post-resolution),
    the renderer surfaces an Outcome column and a theoretical-PnL column,
    plus an aggregate stats footer."""
    selections = [
        {
            "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.7753, "fair_odds": 1.290,
            "tier": "B",
            "game_time_iso": "2026-05-09T14:20:00+00:00",
            "status": "WIN", "theoretical_pnl": 7.25, "result_winner": "Shelton B.",
        },
        {
            "pick": "Tier B Loser", "opponent": "Underdog",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.72, "fair_odds": 1.389,
            "tier": "B",
            "game_time_iso": "2026-05-09T15:00:00+00:00",
            "status": "LOSS", "theoretical_pnl": -25.0, "result_winner": "Underdog",
        },
        {
            "pick": "Tier B Pending", "opponent": "Whoever",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.74, "fair_odds": 1.351,
            "tier": "B",
            "game_time_iso": "2026-05-09T20:00:00+00:00",
            "status": "pending", "theoretical_pnl": 0.0, "result_winner": None,
        },
    ]
    out = render_shadow_picks_block(selections)
    assert "Outcome" in out
    assert "Theo PnL" in out  # column header
    assert "WIN" in out
    assert "LOSS" in out
    assert "pending" in out
    assert "$+7.25" in out
    assert "$-25.00" in out
    # Aggregate footer
    assert "Resolved: 2" in out
    assert "Win rate: 50.0%" in out  # 1W / 2 resolved
    assert "Theoretical PnL: $-17.75" in out  # 7.25 + -25.00


def test_shadow_picks_block_aggregate_skipped_when_no_resolutions():
    """If every shadow pick is still pending, no aggregate footer needed."""
    selections = [{
        "pick": "Future Pick", "opponent": "Other",
        "league": "WTA", "surface": "clay",
        "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
        "game_time_iso": "2026-05-09T22:00:00+00:00",
        "status": "pending", "theoretical_pnl": 0.0,
    }]
    out = render_shadow_picks_block(selections)
    assert "Outcome" in out
    assert "pending" in out
    assert "Resolved:" not in out
    assert "Win rate" not in out


# ── render_placer_rejection_diagnostics_block ────────────────────────────────

from tennis_portfolio import render_placer_rejection_diagnostics_block


def test_placer_rejection_diagnostics_empty():
    """No placer activity in window → empty placeholder."""
    from datetime import datetime, timezone
    out = render_placer_rejection_diagnostics_block(
        placer_skips=[], placed=[],
        now_utc=datetime(2026, 5, 9, 22, tzinfo=timezone.utc),
    )
    assert "_No placer attempts" in out


def test_placer_rejection_diagnostics_aggregates_by_reason_with_pct():
    """Counts each reason + a 'placed' bucket, with percentage of total
    placer attempts. Surfaces which gates are blocking signal capture."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 9, 22, tzinfo=timezone.utc)
    placer_skips = [
        {"reason": "negative_edge", "ts": "2026-05-09T08:45:00+00:00", "source": "placer"},
        {"reason": "negative_edge", "ts": "2026-05-09T12:15:00+00:00", "source": "placer"},
        {"reason": "negative_edge", "ts": "2026-05-08T10:45:00+00:00", "source": "placer"},
        {"reason": "odds_out_of_range_at_placement", "ts": "2026-05-08T08:45:00+00:00", "source": "placer"},
        {"reason": "odds_out_of_range_at_placement", "ts": "2026-05-07T10:15:00+00:00", "source": "placer"},
        {"reason": "no_liquidity", "ts": "2026-05-07T08:45:00+00:00", "source": "placer"},
    ]
    placed = [
        {"ts": "2026-05-08T11:55:00+00:00", "pick": "Djokovic"},  # within 7d
    ]

    out = render_placer_rejection_diagnostics_block(
        placer_skips=placer_skips, placed=placed, now_utc=now, window_days=7,
    )

    assert "## Placer Rejection Diagnostics" in out
    assert "last 7 days" in out
    # 7 total attempts: 6 skips + 1 placed
    assert "negative_edge" in out and "| 3 |" in out
    assert "odds_out_of_range_at_placement" in out and "| 2 |" in out
    assert "no_liquidity" in out and "| 1 |" in out
    assert "placed" in out
    assert "**7**" in out  # total bold


def test_placer_rejection_diagnostics_excludes_events_outside_window():
    """Events older than window_days are not counted."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 9, 22, tzinfo=timezone.utc)
    placer_skips = [
        {"reason": "negative_edge", "ts": "2026-05-09T08:45:00+00:00", "source": "placer"},  # in
        {"reason": "negative_edge", "ts": "2026-04-01T08:45:00+00:00", "source": "placer"},  # out
    ]

    out = render_placer_rejection_diagnostics_block(
        placer_skips=placer_skips, placed=[], now_utc=now, window_days=7,
    )

    # Only 1 in-window event → count should be 1, not 2
    assert "**1**" in out
    assert out.count("negative_edge") == 1 or "| 1 |" in out


def test_placer_rejection_diagnostics_ignores_non_placer_skips():
    """Skipped events without source=placer (e.g. scan-time audit skips) are
    not placement attempts and must not be counted."""
    from datetime import datetime, timezone
    now = datetime(2026, 5, 9, 22, tzinfo=timezone.utc)
    placer_skips = [
        # source=placer counted
        {"reason": "negative_edge", "ts": "2026-05-09T08:45:00+00:00", "source": "placer"},
        # source missing — must NOT be counted
        {"reason": "negative_edge", "ts": "2026-05-09T07:00:00+00:00"},
    ]

    out = render_placer_rejection_diagnostics_block(
        placer_skips=placer_skips, placed=[], now_utc=now, window_days=7,
    )

    assert "**1**" in out  # only one placement attempt counted


def test_today_settlements_winning():
    settlements = [{
        "pick_id": "0xdjk",
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "outcome": "win", "pnl": 12.38,
        "ts": "2026-05-08T15:00:00+00:00",
    }]
    placed = {"0xdjk": {
        "pick_id": "0xdjk", "model_prob": 0.8713,
        "sxbet_odds": 1.4953, "sxbet_available_usd": 1000.0,
        "stake": 25.0,
        "ts": "2026-05-08T11:55:00+00:00",
    }}
    out = render_today_settlements_block(settlements=settlements, placed_lookup=placed)
    assert "Djokovic" in out
    assert "WIN" in out
    assert "$+12.38" in out
    # Kelly P&L should be roughly 76.40*0.4953 = 37.83
    assert "$+37." in out
    # half-K = 152.81 * 0.4953 = 75.66
    assert "$+75." in out


def test_today_settlements_reads_outcome_field_not_won_field():
    """Regression: journal records use `outcome: "win"/"loss"` (string), not `won` (bool).

    Previously the renderer read `s.get("won", False)`, which always returned
    False for real journal records, causing every settlement to display as LOSS.
    """
    settlements = [{
        "pick_id": "0xmedvedev",
        "pick": "Daniil Medvedev", "opponent": "Pablo Llamas Ruiz",
        "outcome": "win", "pnl": 9.96,
        "ts": "2026-05-11T18:02:18+00:00",
    }]
    placed = {"0xmedvedev": {
        "pick_id": "0xmedvedev", "model_prob": 0.8147,
        "sxbet_odds": 1.3986, "sxbet_available_usd": 135.52,
        "stake": 25.0,
        "ts": "2026-05-10T07:00:55+00:00",
    }}
    out = render_today_settlements_block(settlements=settlements, placed_lookup=placed)
    assert "Medvedev" in out
    assert "| WIN |" in out
    assert "LOSS" not in out
    # Base P&L = 25 * (1.3986 - 1) = 9.965 → "$+9.96" or "$+9.97" depending on rounding
    assert "$+9.9" in out
    assert "$-25.00" not in out


def test_yesterday_recap_empty():
    from tennis_portfolio import render_yesterday_recap_block
    out = render_yesterday_recap_block(
        yesterday=date(2026, 5, 11),
        settled_yesterday=[],
        placed_lookup={},
    )
    assert "## Yesterday's Results — 2026-05-11" in out
    assert "No settlements on 2026-05-11" in out


def test_yesterday_recap_with_two_wins():
    from tennis_portfolio import render_yesterday_recap_block
    settled = [
        {
            "pick_id": "0xmedvedev",
            "pick": "Daniil Medvedev", "opponent": "Pablo Llamas Ruiz",
            "outcome": "win", "pnl": 9.96,
            "ts": "2026-05-11T18:02:18+00:00",
        },
        {
            "pick_id": "0xswiatek",
            "pick": "Iga Swiatek", "opponent": "Naomi Osaka",
            "outcome": "win", "pnl": 7.26,
            "ts": "2026-05-11T20:02:20+00:00",
        },
    ]
    placed = {
        "0xmedvedev": {
            "pick_id": "0xmedvedev", "model_prob": 0.8147,
            "sxbet_odds": 1.3986, "sxbet_available_usd": 135.52,
            "stake": 25.0,
        },
        "0xswiatek": {
            "pick_id": "0xswiatek", "model_prob": 0.8504,
            "sxbet_odds": 1.2903, "sxbet_available_usd": 124.26,
            "stake": 25.0,
        },
    }
    out = render_yesterday_recap_block(
        yesterday=date(2026, 5, 11),
        settled_yesterday=settled,
        placed_lookup=placed,
    )
    # Header + both picks rendered
    assert "## Yesterday's Results — 2026-05-11" in out
    assert "Medvedev" in out
    assert "Swiatek" in out
    # Both shown as WIN
    assert out.count("| WIN |") == 2
    assert "LOSS" not in out
    # Day P&L line summarises totals across all three modes
    # Base: 9.97 + 7.26 = 17.22-17.23
    assert "Day P&L" in out
    assert "$+17.2" in out
    # Win/loss summary
    assert "2 W / 0 L" in out


def test_yesterday_recap_mixed_win_loss():
    from tennis_portfolio import render_yesterday_recap_block
    settled = [
        {
            "pick_id": "0xa",
            "pick": "Player A", "opponent": "Player B",
            "outcome": "win", "pnl": 12.38,
            "ts": "2026-05-11T15:00:00+00:00",
        },
        {
            "pick_id": "0xc",
            "pick": "Player C", "opponent": "Player D",
            "outcome": "loss", "pnl": -25.0,
            "ts": "2026-05-11T17:00:00+00:00",
        },
    ]
    placed = {
        "0xa": {"sxbet_odds": 1.4953, "model_prob": 0.8713,
                "sxbet_available_usd": 1000.0, "stake": 25.0},
        "0xc": {"sxbet_odds": 1.5, "model_prob": 0.75,
                "sxbet_available_usd": 1000.0, "stake": 25.0},
    }
    out = render_yesterday_recap_block(
        yesterday=date(2026, 5, 11),
        settled_yesterday=settled,
        placed_lookup=placed,
    )
    assert "1 W / 1 L" in out
    assert "WIN" in out
    assert "LOSS" in out


def test_yesterday_recap_uses_canonical_kelly_stakes_from_replay():
    """¼K stake column must use the actual ¼K day-start balance for that
    UTC day, not the starting $500 approximation."""
    from datetime import date
    from tennis_portfolio import render_yesterday_recap_block

    settled_yesterday = [{
        "pick_id": "p1", "pick": "Aaa", "opponent": "Bbb",
        "outcome": "win", "ts": "2026-05-12T15:00:00+00:00",
    }]
    placed_lookup = {"p1": {
        "pick_id": "p1", "sxbet_odds": 1.5, "model_prob": 0.85,
        "sxbet_available_usd": 500.0, "stake": 25.0,
    }}
    # Replay has the ¼K sleeve at a different balance on 2026-05-12 from
    # $500 — we expect render to honour replay's day_start_balance.
    replay = {
        "base": {"today_pnl": 0.0, "today_start_balance": 500.0},
        "quarter_kelly": {"today_pnl": 0.0, "today_start_balance": 600.0},
        "half_kelly": {"today_pnl": 0.0, "today_start_balance": 700.0},
    }

    out = render_yesterday_recap_block(
        date(2026, 5, 12), settled_yesterday, placed_lookup, replay=replay,
    )

    # ¼ × kelly_fraction(0.85, 1.5) × 600 × (1.5 − 1) — must NOT be 500.
    # kelly_fraction = (0.85*1.5 - 1) / (1.5 - 1) = 0.275/0.5 = 0.55
    # qk_stake = 0.25 * 0.55 * 600 = 82.5; qk_pnl = 82.5 * 0.5 = 41.25
    assert "$+41.25" in out, f"expected $+41.25 in ¼K col, got:\n{out}"


def test_open_picks_header_excludes_unrenderable_rows():
    from tennis_portfolio import render_open_picks_block

    open_picks = {
        "p1": {"pick": "Alpha", "opponent": "Beta", "sxbet_odds": 1.5,
               "model_prob": 0.85, "sxbet_available_usd": 100.0, "edge": 0.1},
        "p2": {"pick": "Gamma", "opponent": "Delta"},  # missing sxbet_odds
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    # One renderable row, header should match.
    assert "### Open Picks (1)" in out
    # The dropped pick is surfaced as a flagged row, not silently lost.
    assert "Gamma" in out
    assert "(incomplete data)" in out


def test_open_picks_incomplete_row_has_matching_column_count():
    """Markdown table requires renderable and incomplete rows to have the
    same cell count — otherwise renderers misalign or drop cells silently."""
    from tennis_portfolio import render_open_picks_block

    open_picks = {
        "p1": {"pick": "Alpha", "opponent": "Beta", "sxbet_odds": 1.5,
               "model_prob": 0.85, "sxbet_available_usd": 100.0, "edge": 0.1},
        "p2": {"pick": "Gamma", "opponent": "Delta"},
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    lines = [ln for ln in out.splitlines() if ln.startswith("|")]
    # First two are header + separator, rest are data rows.
    pipe_counts = [ln.count("|") for ln in lines]
    assert len(set(pipe_counts)) == 1, \
        f"all markdown rows must have same pipe count, got {pipe_counts}"


def test_open_picks_zero_odds_treated_as_incomplete():
    """sxbet_odds == 0 or <=1.0 must NOT crash edge math; treat as incomplete."""
    from tennis_portfolio import render_open_picks_block

    open_picks = {
        "p1": {"pick": "Alpha", "opponent": "Beta", "sxbet_odds": 0.0,
               "model_prob": 0.85, "sxbet_available_usd": 100.0},
        "p2": {"pick": "Gamma", "opponent": "Delta", "sxbet_odds": 1.0,
               "model_prob": 0.85, "sxbet_available_usd": 100.0},
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)  # must not raise
    assert "### Open Picks (0)" in out
    assert "Alpha" in out and "(incomplete data)" in out
    assert "Gamma" in out


def test_yesterday_recap_marks_orphan_settlement():
    """A settlement whose pick_id isn't in placed_lookup must render with
    an explicit (orphan) tag rather than silently computing P&L with odds=0."""
    from datetime import date
    from tennis_portfolio import render_yesterday_recap_block

    settled = [{
        "pick_id": "ghost", "pick": "Mystery M.", "opponent": "Unknown U.",
        "outcome": "win", "ts": "2026-05-12T15:00:00+00:00",
    }]
    placed_lookup: dict = {}  # no matching parent
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_yesterday_recap_block(date(2026, 5, 12), settled, placed_lookup, replay=replay)
    assert "orphan" in out.lower(), f"expected orphan marker, got:\n{out}"


def test_today_settlements_marks_orphan_settlement():
    """Same orphan guard as yesterday recap, but for the today-settlements block."""
    from tennis_portfolio import render_today_settlements_block

    settled = [{
        "pick_id": "ghost", "pick": "Mystery M.", "opponent": "Unknown U.",
        "outcome": "loss", "ts": "2026-05-13T15:00:00+00:00",
    }]
    placed_lookup: dict = {}
    out = render_today_settlements_block(settled, placed_lookup)
    assert "orphan" in out.lower(), f"expected orphan marker, got:\n{out}"


def test_yesterday_recap_marks_retired_pick():
    from datetime import date
    from tennis_portfolio import render_yesterday_recap_block

    settled = [{
        "pick_id": "p1", "pick": "Alpha A.", "opponent": "Bravo B.",
        "outcome": "retired", "ts": "2026-05-12T15:00:00+00:00",
    }]
    placed_lookup = {"p1": {"pick_id": "p1", "sxbet_odds": 1.5, "model_prob": 0.8, "sxbet_available_usd": 500.0}}
    out = render_yesterday_recap_block(date(2026, 5, 12), settled, placed_lookup,
        replay={"base": {"today_start_balance": 500.0},
                "quarter_kelly": {"today_start_balance": 500.0},
                "half_kelly": {"today_start_balance": 500.0}})
    assert "RETIRED" in out
    assert "$0.00" in out


def test_today_settlements_marks_retired_pick():
    from tennis_portfolio import render_today_settlements_block
    settled = [{
        "pick_id": "p1", "pick": "Alpha A.", "opponent": "Bravo B.",
        "outcome": "retired", "ts": "2026-05-13T15:00:00+00:00",
    }]
    placed_lookup = {"p1": {"pick_id": "p1", "sxbet_odds": 1.5, "model_prob": 0.8, "sxbet_available_usd": 500.0}}
    out = render_today_settlements_block(settled, placed_lookup)
    assert "RETIRED" in out
    assert "$0.00" in out
