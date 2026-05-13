"""After Task H, the bot's main loop is settle-only.

run_scan is a deprecated no-op preserved for one release cycle. Pick
creation is owned by tennis_identifier + tennis_placer (cron + at).
"""

from __future__ import annotations

import json

import pytest


def test_run_scan_is_deprecated_no_op(caplog):
    """run_scan returns state unchanged and emits a deprecation warning."""
    import logging
    import tennis_dry_run as tdr

    state = {"open_picks": {}, "today_bets": 0, "today_date": "2026-05-14"}
    with caplog.at_level(logging.WARNING, logger="tennis_dry_run"):
        result = tdr.run_scan(state, executor=None)

    assert result is state, "run_scan must return state unchanged (same object)"
    assert result == {"open_picks": {}, "today_bets": 0, "today_date": "2026-05-14"}
    assert any("deprecated" in r.message.lower() for r in caplog.records), \
        f"run_scan must log a deprecation warning. Got: {[r.message for r in caplog.records]}"


def test_main_loop_does_not_call_run_scan(monkeypatch, tmp_path):
    """The continuous loop in main() must not invoke run_scan in the new
    architecture. Identifier+placer (cron+at) own picking."""
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")
    monkeypatch.setattr(tdr, "SKIPPED_FILE", tmp_path / "skipped.jsonl")
    (tmp_path / "state.json").write_text(json.dumps({
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {}, "today_bets": 0, "today_date": "2026-05-14",
    }), encoding="utf-8")

    scan_count = {"n": 0}
    real_run_scan = tdr.run_scan
    def _spy_run_scan(state, executor=None):
        scan_count["n"] += 1
        return real_run_scan(state, executor)
    monkeypatch.setattr(tdr, "run_scan", _spy_run_scan)
    monkeypatch.setattr(tdr, "scrape_completed_results", lambda *a, **kw: [])
    monkeypatch.setattr("time.sleep", lambda s: None)

    # Run two loop iterations then exit.
    tdr.main(iteration_cap=2)

    assert scan_count["n"] == 0, \
        f"run_scan was called {scan_count['n']} times in the retired loop"


def test_main_loop_still_settles_open_picks(monkeypatch, tmp_path):
    """Settle branch still fires when state has open picks."""
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")
    monkeypatch.setattr(tdr, "SKIPPED_FILE", tmp_path / "skipped.jsonl")

    # load_state binds its default state_file at definition time, so patching
    # STATE_FILE doesn't redirect it. Stub load_state directly.
    monkeypatch.setattr(tdr, "load_state", lambda *a, **kw: {
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {
            "p1": {"pick_id": "p1", "pick": "Player A", "stake": 25.0,
                   "sxbet_odds": 1.5, "mode": "dry_run"},
        },
        "today_bets": 1, "today_date": "2026-05-14",
    })
    monkeypatch.setattr(tdr, "save_state", lambda *a, **kw: None)

    settle_count = {"n": 0}
    def _spy_settle(state, executor):
        settle_count["n"] += 1
        return state, set()
    monkeypatch.setattr(tdr, "run_settle", _spy_settle)
    monkeypatch.setattr("time.sleep", lambda s: None)

    tdr.main(iteration_cap=1)

    assert settle_count["n"] >= 1, "settle did not fire despite open picks"
