"""Placer's atomic post-place state mutator must increment today_bets and
roll over today_date when crossing a UTC midnight (migrated from run_scan
in Task F)."""

from __future__ import annotations

from datetime import datetime, timezone


def test_apply_placement_increments_today_bets_same_day():
    """today_date matches → just increment today_bets and add open_pick."""
    from tennis_placer import _apply_placement_to_state

    state = {
        "today_date": "2026-05-14", "today_bets": 3,
        "open_picks": {}, "balance": 500.0,
    }
    now = datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc)
    trade = {"pick_id": "p1", "pick": "Player A", "stake": 25.0}

    out = _apply_placement_to_state(state, "p1", trade, now)

    assert out["today_bets"] == 4
    assert out["today_date"] == "2026-05-14"
    assert out["open_picks"]["p1"] == trade


def test_apply_placement_rolls_over_at_new_utc_day():
    """today_date is stale → reset today_bets to 0 BEFORE incrementing."""
    from tennis_placer import _apply_placement_to_state

    state = {
        "today_date": "2026-05-13", "today_bets": 9,
        "open_picks": {}, "balance": 500.0,
    }
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)
    trade = {"pick_id": "p2", "pick": "Player B", "stake": 25.0}

    out = _apply_placement_to_state(state, "p2", trade, now)

    assert out["today_bets"] == 1, "today_bets must reset on day rollover before incrementing"
    assert out["today_date"] == "2026-05-14"
    assert out["open_picks"]["p2"] == trade


def test_apply_placement_initialises_today_bets_when_missing():
    """No today_bets key → treat as 0, then increment."""
    from tennis_placer import _apply_placement_to_state

    state = {"open_picks": {}, "balance": 500.0}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)
    trade = {"pick_id": "p3", "pick": "Player C", "stake": 25.0}

    out = _apply_placement_to_state(state, "p3", trade, now)

    assert out["today_bets"] == 1
    assert out["today_date"] == "2026-05-14"


def test_apply_placement_preserves_other_state_fields():
    """Helper must not clobber unrelated keys (balance, total_pnl, wins, etc.)."""
    from tennis_placer import _apply_placement_to_state

    state = {
        "today_date": "2026-05-14", "today_bets": 0,
        "open_picks": {"existing": {"pick_id": "existing"}},
        "balance": 472.50, "total_pnl": -27.50, "wins": 2, "losses": 3,
    }
    now = datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc)
    trade = {"pick_id": "p4", "stake": 25.0}

    out = _apply_placement_to_state(state, "p4", trade, now)

    assert out["balance"] == 472.50
    assert out["total_pnl"] == -27.50
    assert out["wins"] == 2
    assert out["losses"] == 3
    assert "existing" in out["open_picks"]
    assert out["open_picks"]["p4"] == trade
