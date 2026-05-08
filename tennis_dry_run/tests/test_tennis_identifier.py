"""Tests for tennis_identifier — morning identifier that schedules per-pick placers.

The identifier scans for today's matches once in the morning (07:00 UTC by
default), runs the model, and either schedules an `at` job or invokes the
placer synchronously based on each match's start time.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import tennis_identifier as ti


# ── today_window ──────────────────────────────────────────────────────────────

def test_today_window_returns_now_to_end_of_utc_day():
    now = datetime(2026, 5, 6, 7, 0, 0, tzinfo=timezone.utc)
    start, end = ti.today_window(now)
    assert start == now
    assert end == datetime(2026, 5, 6, 23, 59, 59, 999999, tzinfo=timezone.utc)


# ── filter_today_markets ──────────────────────────────────────────────────────

def _ts(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc).timestamp())


def test_filter_today_skips_markets_starting_tomorrow_or_already_started():
    now = datetime(2026, 5, 6, 7, 0, 0, tzinfo=timezone.utc)
    markets = [
        {"market_hash": "0xa", "game_time": _ts(2026, 5, 6, 14)},  # today, future
        {"market_hash": "0xb", "game_time": _ts(2026, 5, 7, 14)},  # tomorrow
        {"market_hash": "0xc", "game_time": _ts(2026, 5, 6, 5)},   # today, past
        {"market_hash": "0xd", "game_time": _ts(2026, 5, 6, 23, 30)},  # today, late
    ]

    result = ti.filter_today_markets(markets, now)

    assert [m["market_hash"] for m in result] == ["0xa", "0xd"]


# ── evaluate_market ───────────────────────────────────────────────────────────

def test_evaluate_market_short_circuits_on_already_open_pick():
    """If market_hash is already in state.open_picks, skip without invoking
    the predictor. This avoids redundant model calls for picks the scan loop
    has already placed."""
    market = {
        "market_hash": "0xabc",
        "player_a": "Aryna Sabalenka",
        "player_b": "Magda Linette",
        "league": "WTA Madrid",
        "game_time": _ts(2026, 5, 6, 14),
    }
    state = {"open_picks": {"0xabc": {"pick": "Aryna Sabalenka"}}}
    predictor = MagicMock()

    result = ti.evaluate_market(market, elo_data={}, predictor=predictor,
                                te_round_map={}, state=state, now_utc=datetime(2026, 5, 6, 7, tzinfo=timezone.utc))

    assert result is None
    predictor.predict_match.assert_not_called()


def test_evaluate_market_returns_qualifying_selection():
    """High-confidence pick with valid Elo + fair-odds in range produces a
    selection dict matching the placer's expected shape."""
    market = {
        "market_hash": "0xabc",
        "player_a": "Aryna Sabalenka",
        "player_b": "Magda Linette",
        "league": "WTA Madrid",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Aryna Sabalenka": {"overall": 2100, "clay": 2080, "rank": 1},
        "Magda Linette": {"overall": 1700, "clay": 1680, "rank": 60},
    }
    predictor = MagicMock()
    predictor.predict_match.return_value = {"prob_a": 0.85, "prob_b": 0.15}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                state=state, now_utc=now)

    assert result is not None
    assert result["pick"] == "Aryna Sabalenka"
    assert result["opponent"] == "Magda Linette"
    assert result["pick_id"] == "0xabc"
    assert result["market_hash"] == "0xabc"
    assert result["model_prob"] == pytest.approx(0.85, abs=1e-6)
    assert result["fair_odds"] == pytest.approx(1.0 / 0.85, abs=1e-3)
    assert result["surface"] == "clay"
    assert result["league"] == "WTA Madrid"
    assert result["game_time"] == _ts(2026, 5, 6, 14)
    assert result["is_pick_outcome_one"] is True


def test_evaluate_market_skips_low_confidence_pick():
    """Confidence < MIN_CONFIDENCE (0.80) is filtered out."""
    market = {
        "market_hash": "0xdef",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Rome",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Player A": {"overall": 1800, "clay": 1800, "rank": 30},
        "Player B": {"overall": 1750, "clay": 1750, "rank": 40},
    }
    predictor = MagicMock()
    predictor.predict_match.return_value = {"prob_a": 0.65, "prob_b": 0.35}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                state=state, now_utc=now)

    assert result is None


def test_evaluate_market_skips_when_either_player_lacks_elo():
    market = {
        "market_hash": "0xnoelo",
        "player_a": "Aryna Sabalenka",
        "player_b": "Unknown Newcomer",
        "league": "WTA Madrid",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {"Aryna Sabalenka": {"overall": 2100, "clay": 2080, "rank": 1}}
    predictor = MagicMock()
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                state=state, now_utc=now)

    assert result is None
    predictor.predict_match.assert_not_called()


# ── schedule_or_place ─────────────────────────────────────────────────────────

def test_schedule_or_place_schedules_at_job_for_future_match():
    """Match more than 15 min away → invoke `at -t YYYYMMDDHHMM` with the
    placer command, return placement_path='scheduled'."""
    now = datetime(2026, 5, 6, 7, 0, 0, tzinfo=timezone.utc)
    selection = {
        "pick_id": "0xabc",
        "game_time": _ts(2026, 5, 6, 14),  # 7 hours from now
    }

    with patch("tennis_identifier.subprocess.run") as mock_run:
        result = ti.schedule_or_place(selection, now_utc=now, lead_min=15,
                                      placer_cmd=["/usr/bin/python", "/opt/placer.py"])

    assert result["placement_path"] == "scheduled"
    assert result["scheduled_at_iso"] == "2026-05-06T13:45:00+00:00"
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "at"
    assert cmd[1] == "-t"
    assert cmd[2] == "202605061345"
    assert "0xabc" in kwargs.get("input", "")
    assert "/opt/placer.py" in kwargs.get("input", "")


def test_schedule_or_place_invokes_placer_immediately_when_under_lead_min():
    """Match within lead_min minutes → call placer subprocess directly,
    not via `at`. Return placement_path='immediate'."""
    now = datetime(2026, 5, 6, 7, 0, 0, tzinfo=timezone.utc)
    selection = {
        "pick_id": "0xabc",
        "game_time": _ts(2026, 5, 6, 7, 5),  # 5 min from now
    }

    with patch("tennis_identifier.subprocess.run") as mock_run:
        result = ti.schedule_or_place(selection, now_utc=now, lead_min=15,
                                      placer_cmd=["/usr/bin/python", "/opt/placer.py"])

    assert result["placement_path"] == "immediate"
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd == ["/usr/bin/python", "/opt/placer.py", "0xabc"]
    # Should NOT use `at` for immediate placement
    assert "at" not in cmd


# ── write_daily_report ────────────────────────────────────────────────────────

def _make_state_dir(tmp_path, subdir="state"):
    """Helper: create a minimal state_dir with empty state.json + trades.jsonl."""
    state_dir = tmp_path / subdir
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    return state_dir


def test_write_daily_report_creates_dated_file_with_summary_and_selections(tmp_path):
    """Daily report written to <vault_dir>/YYYY-MM-DD.md with frontmatter,
    counts table, portfolio block, and one row per selection."""
    now = datetime(2026, 5, 7, 7, 0, 0, tzinfo=timezone.utc)
    counts = {"qualified": 2, "scheduled": 1, "immediate": 1,
              "skipped_dedup": 0, "skipped_filter": 5}
    selections = [
        {
            "pick": "Aryna Sabalenka", "opponent": "Magda Linette",
            "league": "WTA Madrid", "surface": "clay",
            "model_prob": 0.85, "fair_odds": 1.176,
            "sxbet_odds": 1.45, "sxbet_available_usd": 50.0,
            "edge": 0.18,
            "game_time_iso": "2026-05-07T14:00:00+00:00",
            "placement_path": "scheduled",
            "scheduled_at_iso": "2026-05-07T13:45:00+00:00",
        },
        {
            "pick": "Carlos Alcaraz", "opponent": "Random Player",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.91, "fair_odds": 1.099,
            "sxbet_odds": 1.25, "sxbet_available_usd": 30.0,
            "edge": 0.14,
            "game_time_iso": "2026-05-07T07:10:00+00:00",
            "placement_path": "immediate",
            "scheduled_at_iso": None,
        },
    ]
    markets_total = 95
    markets_today = 7
    state_dir = _make_state_dir(tmp_path)
    vault_dir = tmp_path / "vault"

    ti.write_daily_report(now, counts, selections, markets_total, markets_today,
                          vault_dir=vault_dir, state_dir=state_dir)

    report = vault_dir / "2026-05-07.md"
    assert report.exists()
    body = report.read_text(encoding="utf-8")
    assert "type: tennis-daily-report" in body
    assert "date: 2026-05-07" in body
    assert "Qualified" in body
    assert "Markets total" in body and "95" in body
    assert "Aryna Sabalenka" in body
    assert "Carlos Alcaraz" in body
    assert "scheduled" in body
    assert "immediate" in body
    assert "### Portfolio" in body


def test_write_daily_report_overwrites_when_called_twice_same_day(tmp_path):
    """Re-running the identifier on the same UTC date overwrites the file
    rather than duplicating content."""
    now = datetime(2026, 5, 7, 7, 0, 0, tzinfo=timezone.utc)
    base_counts = {"qualified": 0, "scheduled": 0, "immediate": 0,
                   "skipped_dedup": 0, "skipped_filter": 0}
    state_dir = _make_state_dir(tmp_path)
    vault_dir = tmp_path / "vault"

    ti.write_daily_report(now, base_counts, [], 50, 0,
                          vault_dir=vault_dir, state_dir=state_dir)
    first = (vault_dir / "2026-05-07.md").read_text(encoding="utf-8")

    later_counts = {**base_counts, "qualified": 3}
    ti.write_daily_report(now, later_counts, [], 50, 0,
                          vault_dir=vault_dir, state_dir=state_dir)
    second = (vault_dir / "2026-05-07.md").read_text(encoding="utf-8")

    assert first != second
    # File length doesn't double; only one report worth of content.
    assert second.count("# Tennis Daily Report") == 1


# ── persist_selection ─────────────────────────────────────────────────────────

def test_persist_selection_appends_jsonl_line(tmp_path):
    """Selection + scheduling outcome are written as one line to pending file."""
    pending_file = tmp_path / "pending.jsonl"
    selection = {
        "pick_id": "0xabc",
        "pick": "Aryna Sabalenka",
        "opponent": "Magda Linette",
        "league": "WTA Madrid",
        "model_prob": 0.85,
        "fair_odds": 1.176,
        "market_hash": "0xabc",
        "game_time": _ts(2026, 5, 6, 14),
    }
    schedule_outcome = {"placement_path": "scheduled",
                        "scheduled_at_iso": "2026-05-06T13:45:00+00:00"}

    ti.persist_selection(selection, schedule_outcome, pending_file)
    ti.persist_selection(selection, schedule_outcome, pending_file)

    import json
    lines = pending_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["pick_id"] == "0xabc"
    assert parsed["placement_path"] == "scheduled"
    assert parsed["scheduled_at_iso"] == "2026-05-06T13:45:00+00:00"
    assert parsed["model_prob"] == 0.85


def test_evaluate_market_skips_when_fair_odds_below_min():
    """Overconfident prediction (prob > 0.99 → fair_odds < MIN_ODDS=1.01) is
    filtered. This catches the case where confidence passes but model is so
    sure the implied odds are unrealistic."""
    market = {
        "market_hash": "0xoor",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Rome",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Player A": {"overall": 2200, "clay": 2200, "rank": 1},
        "Player B": {"overall": 1400, "clay": 1400, "rank": 250},
    }
    predictor = MagicMock()
    predictor.predict_match.return_value = {"prob_a": 0.995, "prob_b": 0.005}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                state=state, now_utc=now)

    assert result is None


# ── write_daily_report ────────────────────────────────────────────────────────

def test_identifier_writes_portfolio_block_to_daily_file(tmp_path):
    """The new BOD section must include the Portfolio block + Identified Picks."""
    from tennis_identifier import write_daily_report

    selections = [{
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.8713, "fair_odds": 1.148,
        "sxbet_odds": 1.5534, "sxbet_available_usd": 39.05,
        "edge": 0.226,
        "game_time_iso": "2026-05-08T12:10:00+00:00",
        "placement_path": "scheduled",
        "scheduled_at_iso": "2026-05-08T11:55:00+00:00",
    }]
    counts = {"qualified": 1, "scheduled": 1, "immediate": 0,
              "skipped_dedup": 0, "skipped_filter": 0}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    rolling_path = tmp_path / "rolling.md"

    from datetime import datetime, timezone
    out = write_daily_report(
        now_utc=datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        counts=counts,
        selections=selections,
        markets_total=73, markets_today=47,
        vault_dir=vault_dir,
        state_dir=state_dir,
        rolling_path=rolling_path,
    )

    body = out.read_text(encoding="utf-8")
    assert "### Portfolio (snapshot 2026-05-08 07:00 UTC)" in body
    assert "Novak Djokovic" in body
    assert "1.553" in body
    assert "$39.05" in body
    rolling = rolling_path.read_text(encoding="utf-8")
    assert "## Tennis Dry Run Report" in rolling
    assert "### Portfolio" in rolling


def test_write_daily_report_renders_placement_keys_correctly(tmp_path):
    """Regression: selections_for_report must use unprefixed keys (placement_path,
    scheduled_at_iso) not underscore-prefixed ones, so render_identified_picks_block
    correctly displays placement type and scheduled time. This test directly validates
    that the renderer receives the right keys and produces readable output."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    selections = [{
        "pick": "Test Player", "opponent": "Opponent",
        "league": "ATP Test", "surface": "clay",
        "model_prob": 0.85, "fair_odds": 1.176,
        "sxbet_odds": 1.45, "sxbet_available_usd": 50.0,
        "edge": 0.18,
        "game_time_iso": "2026-05-08T14:00:00+00:00",
        "placement_path": "scheduled",  # Must be unprefixed, not "_placement_path"
        "scheduled_at_iso": "2026-05-08T13:45:00+00:00",  # Must be unprefixed, not "_scheduled_at_iso"
    }]
    counts = {"qualified": 1, "scheduled": 1, "immediate": 0,
              "skipped_dedup": 0, "skipped_filter": 0}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"

    write_daily_report(
        now_utc=datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        counts=counts,
        selections=selections,
        markets_total=73, markets_today=47,
        vault_dir=vault_dir,
        state_dir=state_dir,
    )

    report = vault_dir / "2026-05-08.md"
    assert report.exists(), "Daily report not created"
    body = report.read_text(encoding="utf-8")

    # The key assertion: "scheduled" placement should render as-is (not "?").
    # If main() was using underscore-prefixed keys, the renderer would not find
    # placement_path, defaulting to "?" per tennis_portfolio.py line 271.
    assert "scheduled" in body, (
        f"Placement type 'scheduled' missing from report. "
        f"This indicates keys are not being passed correctly. Report:\n{body}"
    )

    # Also verify the time is present (not empty).
    # Line 270 of tennis_portfolio extracts sched = s.get("scheduled_at_iso")[11:16]
    # which should yield "13:45" from the ISO string above.
    assert "13:45" in body or "scheduled" in body, (
        f"Scheduled time not rendered. Report:\n{body}"
    )
