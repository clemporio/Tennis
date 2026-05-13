"""Deployed-accounting tests for replay_three_bankrolls — duplicate open
entries must not inflate the running 'deployed' figure."""

from __future__ import annotations

from datetime import date

import pytest

from tennis_kelly import replay_three_bankrolls


def _open(pick_id: str, ts: str, *, prob: float = 0.85, odds: float = 1.5,
          stake: float = 25.0, avail: float = 500.0) -> dict:
    return {
        "type": "open", "pick_id": pick_id, "ts": ts,
        "model_prob": prob, "sxbet_odds": odds,
        "sxbet_available_usd": avail, "stake": stake,
    }


def _settled(pick_id: str, ts: str, *, won: bool, odds: float = 1.5,
             stake: float = 25.0) -> dict:
    pnl = stake * (odds - 1.0) if won else -stake
    return {
        "type": "settled", "pick_id": pick_id, "ts": ts,
        "outcome": "win" if won else "loss",
        "sxbet_odds": odds, "stake": stake, "pnl": pnl,
    }


def test_duplicate_open_does_not_inflate_deployed_after_settle():
    """Two opens for the same pick_id + one settle → deployed should be 0
    in every sleeve. Today it returns the stake amount (over-count)."""
    placed = [
        _open("p1", "2026-05-08T11:55:00+00:00"),
        _open("p1", "2026-05-08T14:00:00+00:00"),  # duplicate
    ]
    settled = [_settled("p1", "2026-05-08T19:00:00+00:00", won=False)]

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 9))

    assert replay["base"]["deployed"] == pytest.approx(0.0), \
        f"base deployed should be 0 after settle, got {replay['base']['deployed']}"
    assert replay["quarter_kelly"]["deployed"] == pytest.approx(0.0), \
        f"¼K deployed should be 0 after settle, got {replay['quarter_kelly']['deployed']}"
    assert replay["half_kelly"]["deployed"] == pytest.approx(0.0), \
        f"½K deployed should be 0 after settle, got {replay['half_kelly']['deployed']}"


def test_duplicate_open_still_in_open_picks_deployed_equals_one_stake():
    """Two opens for same pick_id, no settle → deployed should equal ONE
    stake, not two (the bot only has one position)."""
    placed = [
        _open("p1", "2026-05-08T11:55:00+00:00"),
        _open("p1", "2026-05-08T14:00:00+00:00"),
    ]
    settled: list[dict] = []

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 8))

    assert replay["base"]["deployed"] == pytest.approx(25.0)


def test_distinct_opens_deployed_sums_correctly():
    """Two opens for DIFFERENT pick_ids → deployed should sum both."""
    placed = [
        _open("p1", "2026-05-08T11:55:00+00:00"),
        _open("p2", "2026-05-08T14:00:00+00:00"),
    ]
    settled = [_settled("p1", "2026-05-08T19:00:00+00:00", won=True)]

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 9))

    assert replay["base"]["deployed"] == pytest.approx(25.0)
