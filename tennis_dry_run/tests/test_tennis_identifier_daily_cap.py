"""Identifier respects the daily-bets cap migrated from run_scan (Task F).

After Task H retirement, the bot service no longer runs run_scan and so no
longer enforces MAX_DAILY_BETS. The identifier inherits that responsibility.

Cap math: cap = env override or MAX_DAILY_BETS; remaining = cap - already_today
- qualified_this_run, with already_today=0 when state.today_date is stale.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_daily_cap_remaining_full_cap_when_state_empty():
    """No today_bets, no qualified this run → full cap remaining."""
    from tennis_identifier import _daily_cap_remaining

    state = {"open_picks": {}, "today_bets": 0, "today_date": "2026-05-14"}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)

    assert _daily_cap_remaining(state, qualified_this_run=0, now_utc=now, cap=10) == 10


def test_daily_cap_remaining_subtracts_today_bets_and_qualified():
    """remaining = cap - already_today - qualified_this_run."""
    from tennis_identifier import _daily_cap_remaining

    state = {"today_bets": 4, "today_date": "2026-05-14"}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)

    assert _daily_cap_remaining(state, qualified_this_run=2, now_utc=now, cap=10) == 4


def test_daily_cap_remaining_zero_when_cap_hit():
    """qualified + today_bets >= cap → 0 remaining, clamped non-negative."""
    from tennis_identifier import _daily_cap_remaining

    state = {"today_bets": 10, "today_date": "2026-05-14"}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)

    assert _daily_cap_remaining(state, qualified_this_run=0, now_utc=now, cap=10) == 0
    assert _daily_cap_remaining(state, qualified_this_run=5, now_utc=now, cap=10) == 0


def test_daily_cap_remaining_treats_stale_today_date_as_zero():
    """If state.today_date is yesterday, today_bets is stale → ignored."""
    from tennis_identifier import _daily_cap_remaining

    state = {"today_bets": 9, "today_date": "2026-05-13"}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)

    assert _daily_cap_remaining(state, qualified_this_run=0, now_utc=now, cap=10) == 10


def test_daily_cap_remaining_missing_today_date_treated_as_stale():
    """No today_date in state → treat as no today_bets accrued today."""
    from tennis_identifier import _daily_cap_remaining

    state = {"today_bets": 7}
    now = datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc)

    assert _daily_cap_remaining(state, qualified_this_run=0, now_utc=now, cap=10) == 10
