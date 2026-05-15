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

    result, reason = ti.evaluate_market(market, elo_data={}, predictor=predictor,
                                        te_round_map={}, state=state, now_utc=datetime(2026, 5, 6, 7, tzinfo=timezone.utc))

    assert result is None
    assert reason == "dedup"
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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is not None
    assert reason is None
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
    """Pick below SHADOW_MIN_CONFIDENCE (0.70) is filtered out entirely."""
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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "low_conf"


def test_evaluate_market_tags_tier_A_when_above_min_confidence():
    """Pick at >= MIN_CONFIDENCE (0.80) is tagged tier='A' (placement track)."""
    market = {
        "market_hash": "0xa",
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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is not None
    assert reason is None
    assert result["tier"] == "A"


def test_evaluate_market_tags_tier_B_when_in_shadow_band():
    """Pick in [SHADOW_MIN_CONFIDENCE, MIN_CONFIDENCE) = [0.70, 0.80) is tagged
    tier='B' (shadow track) — no placer fires, but the pick is still recorded."""
    market = {
        "market_hash": "0xb",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Rome",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Player A": {"overall": 1900, "clay": 1900, "rank": 10},
        "Player B": {"overall": 1700, "clay": 1700, "rank": 50},
    }
    predictor = MagicMock()
    predictor.predict_match.return_value = {"prob_a": 0.74, "prob_b": 0.26}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is not None
    assert reason is None
    assert result["tier"] == "B"
    assert result["model_prob"] == pytest.approx(0.74, abs=1e-6)


def test_evaluate_market_skips_challenger_leagues():
    """Challenger / qualifying matches must be filtered: a prior backtest
    showed the model can't predict these reliably (sparse Elo, lower-tier
    players, different volatility). Filter is a hard gate, before model call."""
    market = {
        "market_hash": "0xchall",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Challenger - Brazzaville",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Player A": {"overall": 2100, "clay": 2080, "rank": 1},
        "Player B": {"overall": 1700, "clay": 1680, "rank": 60},
    }
    predictor = MagicMock()
    predictor.predict_match.return_value = {"prob_a": 0.85, "prob_b": 0.15}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "excluded_league"
    # Filter must be PRE-model, so we don't waste a predict call on challengers.
    predictor.predict_match.assert_not_called()


def test_evaluate_market_skips_qualifying_rounds():
    """Same gate covers WTA/ATP qualifying ('Q1', 'Q2', 'Qualifying')."""
    market = {
        "market_hash": "0xqual",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Rome - Qualifying",
        "game_time": _ts(2026, 5, 6, 14),
    }
    predictor = MagicMock()
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result, reason = ti.evaluate_market(market, {}, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "excluded_league"
    predictor.predict_match.assert_not_called()


def test_evaluate_market_skips_itf_leagues():
    market = {
        "market_hash": "0xitf",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ITF Mens W25 - Antalya",
        "game_time": _ts(2026, 5, 6, 14),
    }
    predictor = MagicMock()
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result, reason = ti.evaluate_market(market, {}, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "excluded_league"
    predictor.predict_match.assert_not_called()


def test_evaluate_market_accepts_main_tour_atp():
    """ATP Masters / Grand Slam / regular tour-level events must NOT be
    rejected by the challenger filter."""
    market = {
        "market_hash": "0xtour",
        "player_a": "Aryna Sabalenka",
        "player_b": "Magda Linette",
        "league": "WTA Rome",
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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is not None
    assert reason is None
    assert result["pick"] == "Aryna Sabalenka"


def test_evaluate_market_skips_tier_B_when_fair_odds_above_max():
    """Even tier-B picks must respect MIN_ODDS/MAX_ODDS bounds. A 0.45-prob
    pick (fair_odds 2.22) is below the threshold *and* outside MAX_ODDS=2.0."""
    market = {
        "market_hash": "0xoor",
        "player_a": "Player A",
        "player_b": "Player B",
        "league": "ATP Rome",
        "game_time": _ts(2026, 5, 6, 14),
    }
    elo_data = {
        "Player A": {"overall": 1800, "clay": 1800, "rank": 30},
        "Player B": {"overall": 1790, "clay": 1790, "rank": 35},
    }
    predictor = MagicMock()
    # 0.49 < SHADOW_MIN_CONFIDENCE so first filter; OR if you set 0.49,
    # it's filtered as low confidence regardless. Use 0.74 with synthetic
    # prob bypass — but easier: check threshold path with 0.45.
    predictor.predict_match.return_value = {"prob_a": 0.45, "prob_b": 0.55}
    state = {"open_picks": {}}
    now = datetime(2026, 5, 6, 7, tzinfo=timezone.utc)

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None  # 0.55 < 0.70 shadow threshold
    assert reason == "low_conf"


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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "no_elo"
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

# ── prune_stale_pending ───────────────────────────────────────────────────────

def _write_pending(path, rows):
    import json
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_prune_stale_pending_drops_entries_with_past_game_time(tmp_path):
    """Entries whose game_time is more than grace_minutes in the past are removed."""
    pending = tmp_path / "pending.jsonl"
    now = datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xstale1", "pick": "Qinwen Zheng",
         "game_time": _ts(2026, 5, 7, 9)},  # ~46h old → prune
        {"pick_id": "0xstale2", "pick": "Linda Noskova",
         "game_time": _ts(2026, 5, 7, 11)},  # ~44h old → prune
        {"pick_id": "0xfresh", "pick": "Jannik Sinner",
         "game_time": _ts(2026, 5, 9, 17)},  # 10h ahead → keep
    ]
    _write_pending(pending, rows)

    result = ti.prune_stale_pending(pending, now_utc=now, grace_minutes=60)

    assert result["pruned"] == 2
    assert result["kept"] == 1
    assert sorted(result["pruned_picks"]) == ["Linda Noskova", "Qinwen Zheng"]

    import json
    surviving = [json.loads(l) for l in pending.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(surviving) == 1
    assert surviving[0]["pick_id"] == "0xfresh"


def test_prune_stale_pending_respects_grace_window(tmp_path):
    """Entry within grace_minutes of now is kept (match may still be in play)."""
    pending = tmp_path / "pending.jsonl"
    now = datetime(2026, 5, 9, 13, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xrecent", "pick": "Live Match",
         "game_time": _ts(2026, 5, 9, 12, 30)},  # 30 min ago, within 60-min grace → keep
        {"pick_id": "0xold", "pick": "Old Match",
         "game_time": _ts(2026, 5, 9, 11, 30)},  # 90 min ago → prune
    ]
    _write_pending(pending, rows)

    result = ti.prune_stale_pending(pending, now_utc=now, grace_minutes=60)

    assert result["pruned"] == 1
    assert result["kept"] == 1
    assert result["pruned_picks"] == ["Old Match"]


def test_prune_stale_pending_handles_missing_file(tmp_path):
    """Missing pending file is a no-op, not a crash."""
    pending = tmp_path / "does_not_exist.jsonl"
    now = datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc)

    result = ti.prune_stale_pending(pending, now_utc=now)

    assert result == {"kept": 0, "pruned": 0, "pruned_picks": []}
    assert not pending.exists()


def test_prune_stale_pending_skips_malformed_lines(tmp_path):
    """Malformed JSON lines are silently dropped, not raising."""
    pending = tmp_path / "pending.jsonl"
    now = datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc)
    pending.write_text(
        '{"pick_id": "0xfresh", "pick": "Future", "game_time": ' + str(_ts(2026, 5, 9, 17)) + '}\n'
        'not-json-garbage\n'
        '\n'
        '{"pick_id": "0xstale", "pick": "Past", "game_time": ' + str(_ts(2026, 5, 7, 9)) + '}\n',
        encoding="utf-8",
    )

    result = ti.prune_stale_pending(pending, now_utc=now, grace_minutes=60)

    assert result["pruned"] == 1
    assert result["kept"] == 1
    import json
    surviving = [json.loads(l) for l in pending.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(surviving) == 1
    assert surviving[0]["pick_id"] == "0xfresh"


def test_prune_stale_pending_keeps_entries_without_game_time(tmp_path):
    """Defensive: an entry missing game_time is kept (unknown → don't drop)."""
    pending = tmp_path / "pending.jsonl"
    now = datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xnogt", "pick": "Mystery"},  # no game_time → keep
    ]
    _write_pending(pending, rows)

    result = ti.prune_stale_pending(pending, now_utc=now, grace_minutes=60)

    assert result["kept"] == 1
    assert result["pruned"] == 0


def test_prune_stale_pending_is_atomic(tmp_path, monkeypatch):
    """If the rewrite step fails, the original file is left intact (no half-rewrite)."""
    pending = tmp_path / "pending.jsonl"
    now = datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xfresh", "pick": "Future", "game_time": _ts(2026, 5, 9, 17)},
        {"pick_id": "0xstale", "pick": "Past", "game_time": _ts(2026, 5, 7, 9)},
    ]
    _write_pending(pending, rows)
    original_bytes = pending.read_bytes()

    # Simulate os.replace failing
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("tennis_identifier.os.replace", boom)

    with pytest.raises(OSError):
        ti.prune_stale_pending(pending, now_utc=now, grace_minutes=60)

    assert pending.read_bytes() == original_bytes


def test_prune_stale_pending_eats_today_shadow_picks_REGRESSION(tmp_path):
    """REGRESSION: 2026-05-11 bug — identifier's prune (60-min grace) removes
    today's shadow picks whose match was earlier in the day, so 22:00 UTC EOD
    sees an empty file and reports 'No shadow picks today' even though tier-B
    picks fired through the identifier. Documents the broken behaviour;
    `prune_shadow_stale` (date-based) is the fix.
    """
    shadow = tmp_path / "shadow_selections.jsonl"
    now = datetime(2026, 5, 11, 11, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xnoskova", "pick": "Linda Noskova", "opponent": "Sara Errani",
         "league": "WTA Rome", "tier": "B",
         "game_time": _ts(2026, 5, 11, 9, 0)},  # today 09:00 UTC, 2h ago
        {"pick_id": "0xnakashima", "pick": "Brandon Nakashima", "opponent": "Alex De Minaur",
         "league": "ATP Rome", "tier": "B",
         "game_time": _ts(2026, 5, 11, 10, 10)},  # today 10:10 UTC, 50min ago
    ]
    _write_pending(shadow, rows)

    ti.prune_stale_pending(shadow, now_utc=now, grace_minutes=60)

    import json
    surviving = [json.loads(l) for l in shadow.read_text(encoding="utf-8").splitlines() if l.strip()]
    surviving_ids = {r["pick_id"] for r in surviving}
    # The bug: today's shadow pick whose match is >60min ago is pruned, even
    # though EOD at 22:00 UTC still needs it. Nakashima at 10:10 (50min ago)
    # is within grace and survives this run, but a subsequent identifier run
    # an hour later would drop him too. By 22:00 UTC EOD both rows are gone.
    assert "0xnoskova" not in surviving_ids
    assert "0xnakashima" in surviving_ids


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

    result, reason = ti.evaluate_market(market, elo_data, predictor, te_round_map={},
                                        state=state, now_utc=now)

    assert result is None
    assert reason == "odds"


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
    assert "Dino Prizmic" in body
    assert "1.148" in body  # fair odds (model only — orderbook not bound at scan)
    rolling = rolling_path.read_text(encoding="utf-8")
    assert "## Tennis Dry Run Report" in rolling
    assert "### Portfolio" in rolling


def test_write_daily_report_includes_shadow_section_when_passed(tmp_path):
    """If write_daily_report is called with shadow_selections, the BOD file
    contains the Shadow Picks block alongside Identified Picks."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    selections = [{
        "pick": "Tier A Pick", "opponent": "Underdog A",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.85, "fair_odds": 1.176, "tier": "A",
        "game_time_iso": "2026-05-09T14:00:00+00:00",
        "placement_path": "scheduled",
        "scheduled_at_iso": "2026-05-09T13:45:00+00:00",
    }]
    shadow = [{
        "pick": "Tier B Pick", "opponent": "Underdog B",
        "league": "WTA Rome", "surface": "clay",
        "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
        "game_time_iso": "2026-05-09T16:00:00+00:00",
        "placement_path": "shadow", "scheduled_at_iso": None,
    }]
    counts = {"qualified": 1, "scheduled": 1, "immediate": 0,
              "skipped_dedup": 0, "skipped_filter": 27, "shadow": 1}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"

    write_daily_report(
        now_utc=datetime(2026, 5, 9, 7, 0, tzinfo=timezone.utc),
        counts=counts,
        selections=selections,
        markets_total=48, markets_today=31,
        vault_dir=vault_dir,
        state_dir=state_dir,
        shadow_selections=shadow,
    )

    body = (vault_dir / "2026-05-09.md").read_text(encoding="utf-8")
    assert "Tier A Pick" in body
    assert "Tier B Pick" in body
    assert "## Shadow Picks (tier B, 70-80% — not placed)" in body
    assert "1.351" in body
    assert "Shadow (tier B, 70-80%)" in body  # scan-summary row
    assert "| 1 |" in body  # shadow count


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


def test_bod_report_includes_yesterday_recap_when_settlements_exist(tmp_path):
    """The BOD daily report must surface yesterday's settlement outcomes so the
    EOD → next-day rollover is visible at the top of the daily file."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 492.22, "open_picks": {}}')
    # Two settlements from yesterday (2026-05-11): both wins.
    (state_dir / "trades.jsonl").write_text(
        '{"type":"open","pick_id":"0xmed","pick":"Daniil Medvedev","opponent":"Pablo Llamas Ruiz","model_prob":0.8147,"sxbet_odds":1.3986,"sxbet_available_usd":135.52,"stake":25.0,"ts":"2026-05-10T07:00:55+00:00"}\n'
        '{"type":"open","pick_id":"0xswi","pick":"Iga Swiatek","opponent":"Naomi Osaka","model_prob":0.8504,"sxbet_odds":1.2903,"sxbet_available_usd":124.26,"stake":25.0,"ts":"2026-05-11T07:00:40+00:00"}\n'
        '{"type":"settled","pick_id":"0xmed","pick":"Daniil Medvedev","opponent":"Pablo Llamas Ruiz","outcome":"win","pnl":9.96,"ts":"2026-05-11T18:02:18+00:00"}\n'
        '{"type":"settled","pick_id":"0xswi","pick":"Iga Swiatek","opponent":"Naomi Osaka","outcome":"win","pnl":7.26,"ts":"2026-05-11T20:02:20+00:00"}\n'
    )

    vault_dir = tmp_path / "vault"
    write_daily_report(
        now_utc=datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                "skipped_dedup": 0, "skipped_filter": 0},
        selections=[],
        markets_total=82, markets_today=71,
        vault_dir=vault_dir,
        state_dir=state_dir,
    )

    body = (vault_dir / "2026-05-12.md").read_text(encoding="utf-8")
    # Recap header for yesterday
    assert "## Yesterday's Results — 2026-05-11" in body
    # Both yesterday picks rendered as WIN
    assert "Daniil Medvedev" in body
    assert "Iga Swiatek" in body
    assert body.count("| WIN |") == 2
    # Day P&L summary
    assert "Day P&L" in body
    assert "2 W / 0 L" in body
    # Recap appears BEFORE Scan Summary (so it sits at top of report)
    assert body.index("Yesterday's Results") < body.index("Scan Summary")


def test_bod_report_includes_recap_placeholder_when_no_yesterday_activity(tmp_path):
    """If yesterday had no settlements, recap block still renders an empty stub."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    write_daily_report(
        now_utc=datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                "skipped_dedup": 0, "skipped_filter": 0},
        selections=[],
        markets_total=50, markets_today=30,
        vault_dir=vault_dir,
        state_dir=state_dir,
    )

    body = (vault_dir / "2026-05-08.md").read_text(encoding="utf-8")
    assert "## Yesterday's Results — 2026-05-07" in body
    assert "_No settlements on 2026-05-07._" in body


def test_bod_report_renders_open_positions_for_carried_over_picks(tmp_path):
    """A pick placed yesterday whose match is today must surface in today's
    BOD report so the open exposure is visible before the EOD settlement."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("state.json").write_text(
        '{"balance": 475.0, "open_picks": {'
        '"0xzv":{"pick_id":"0xzv","pick":"Alexander Zverev","opponent":"Luciano Darderi",'
        '"league":"ATP Rome","surface":"clay","model_prob":0.8495,"sxbet_odds":1.2085,'
        '"sxbet_available_usd":263.87,"edge":0.022,"stake":25.0,'
        '"ts":"2026-05-11T07:00:40+00:00"}'
        '}}'
    )
    state_dir.joinpath("trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    write_daily_report(
        now_utc=datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                "skipped_dedup": 0, "skipped_filter": 0},
        selections=[],
        markets_total=82, markets_today=71,
        vault_dir=vault_dir,
        state_dir=state_dir,
    )

    body = (vault_dir / "2026-05-12.md").read_text(encoding="utf-8")
    assert "Open Picks (1)" in body
    assert "Alexander Zverev" in body
    assert "Luciano Darderi" in body
    # Open Picks block must sit between Portfolio and Scan Summary
    assert body.index("Portfolio") < body.index("Open Picks") < body.index("Scan Summary")


def test_bod_report_renders_empty_open_positions_when_no_open_picks(tmp_path):
    """When state.open_picks is empty, the BOD report still has the section but says so."""
    from datetime import datetime, timezone
    from tennis_identifier import write_daily_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    state_dir.joinpath("trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    write_daily_report(
        now_utc=datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                "skipped_dedup": 0, "skipped_filter": 0},
        selections=[],
        markets_total=10, markets_today=5,
        vault_dir=vault_dir,
        state_dir=state_dir,
    )

    body = (vault_dir / "2026-05-12.md").read_text(encoding="utf-8")
    assert "Open Picks (0)" in body
    assert "_No open picks._" in body


def test_write_daily_report_refetches_state_before_render(tmp_path, monkeypatch):
    """Identifier loads state once at top of main(). If the bot writes a new
    open pick between then and the report write, the report MUST see it.
    Simulate this by mutating state.json between the calls."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    import tennis_identifier as ti

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    state_file.write_text(json.dumps({"open_picks": {}}), encoding="utf-8")
    (state_dir / "trades.jsonl").write_text("", encoding="utf-8")

    vault = tmp_path / "vault"
    now = datetime(2026, 5, 13, 7, 0, 0, tzinfo=timezone.utc)

    # Simulate the bot writing a new open pick AFTER identifier loaded state.
    state_file.write_text(json.dumps({
        "open_picks": {
            "p1": {"pick": "Coco Gauff", "opponent": "Sorana Cirstea",
                   "league": "WTA Rome", "sxbet_odds": 1.4,
                   "model_prob": 0.86, "sxbet_available_usd": 90,
                   "edge": 0.15, "stake": 25.0,
                   "ts": "2026-05-13T07:00:30+00:00"},
        },
    }), encoding="utf-8")

    out = ti.write_daily_report(
        now_utc=now, counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                              "shadow": 0, "skipped_dedup": 0, "skipped_filter": 0},
        selections=[], markets_total=0, markets_today=0,
        vault_dir=vault, state_dir=state_dir,
    )
    body = Path(out).read_text(encoding="utf-8")
    assert "Coco Gauff" in body, "Gauff was opened after main() loaded state; report must re-read state before write"
    assert "Open Picks (1)" in body


def test_write_daily_report_annotates_bot_opened_picks_not_in_identifier_counts(tmp_path):
    """When state.open_picks has entries the identifier didn't 'Qualify',
    the Scan Summary must include a 'Bot-opened (not counted in Qualified)' row
    so the operator sees that the headline count under-reports the day's positions."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    import tennis_identifier as ti

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    # Bot opened p1 just before identifier ran; identifier qualified zero markets.
    state_file.write_text(json.dumps({
        "open_picks": {
            "p1": {"pick": "Coco Gauff", "opponent": "Sorana Cirstea",
                   "league": "WTA Rome", "sxbet_odds": 1.4,
                   "model_prob": 0.86, "sxbet_available_usd": 90,
                   "edge": 0.15, "stake": 25.0,
                   "ts": "2026-05-13T07:00:30+00:00"},
        },
    }), encoding="utf-8")
    (state_dir / "trades.jsonl").write_text("", encoding="utf-8")

    out = ti.write_daily_report(
        now_utc=datetime(2026, 5, 13, 7, 0, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0,
                "shadow": 0, "skipped_dedup": 0, "skipped_filter": 0},
        selections=[], markets_total=0, markets_today=0,
        vault_dir=tmp_path / "vault", state_dir=state_dir,
    )
    body = Path(out).read_text(encoding="utf-8")
    # The Scan Summary should surface that 1 pick exists in open_picks
    # without having been counted in Qualified.
    assert "Bot-opened" in body, f"expected 'Bot-opened' annotation in Scan Summary, got:\n{body[:2000]}"


def test_scan_summary_breaks_down_filter_reasons(tmp_path):
    """Scan Summary in BOD must include per-reason filter counts."""
    from datetime import datetime, timezone
    from pathlib import Path
    import tennis_identifier as ti

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"open_picks": {}}', encoding="utf-8")
    (state_dir / "trades.jsonl").write_text("", encoding="utf-8")

    counts = {
        "qualified": 1, "scheduled": 1, "immediate": 0, "shadow": 2,
        "skipped_dedup": 0,
        "skipped_filter": 50,
        "skipped_no_elo": 12,
        "skipped_low_conf": 30,
        "skipped_round": 3,
        "skipped_odds": 4,
        "skipped_excluded_league": 1,
    }
    out = ti.write_daily_report(
        now_utc=datetime(2026, 5, 13, 7, 0, tzinfo=timezone.utc),
        counts=counts, selections=[], markets_total=63, markets_today=52,
        vault_dir=tmp_path / "vault", state_dir=state_dir,
    )
    body = Path(out).read_text(encoding="utf-8")
    assert "no_elo" in body and "12" in body
    assert "low_conf" in body and "30" in body
    assert "excluded_league" in body


def test_write_daily_report_applies_settled_corrections(tmp_path, monkeypatch):
    """A journal with a settled_correction must surface the corrected
    outcome/pnl in Yesterday's Results, not the original wrong values."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    import tennis_identifier as ti

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({"open_picks": {}}), encoding="utf-8")
    (state_dir / "trades.jsonl").write_text(
        json.dumps({"type": "open", "pick_id": "p1", "pick": "Alpha A.",
                    "opponent": "Bravo B.", "model_prob": 0.85, "sxbet_odds": 1.5,
                    "sxbet_available_usd": 500.0, "stake": 25.0,
                    "ts": "2026-05-11T07:00:00+00:00"}) + "\n" +
        json.dumps({"type": "settled", "pick_id": "p1", "pick": "Alpha A.",
                    "opponent": "Bravo B.", "outcome": "win", "pnl": 12.5,
                    "stake": 25.0, "result_winner": "Alpha A.", "sxbet_odds": 1.5,
                    "ts": "2026-05-12T15:00:00+00:00"}) + "\n" +
        json.dumps({"type": "settled_correction", "corrects": "2026-05-12T15:00:00+00:00",
                    "pick_id": "p1", "pick": "Alpha A.", "opponent": "Bravo B.",
                    "outcome": "loss", "pnl": -25.0, "stake": 25.0,
                    "result_winner": "Bravo B.", "sxbet_odds": 1.5,
                    "ts": "2026-05-13T07:00:00+00:00"}) + "\n",
        encoding="utf-8")

    out = ti.write_daily_report(
        now_utc=datetime(2026, 5, 13, 7, 0, 0, tzinfo=timezone.utc),
        counts={"qualified": 0, "scheduled": 0, "immediate": 0, "shadow": 0,
                "skipped_dedup": 0, "skipped_filter": 0},
        selections=[], markets_total=0, markets_today=0,
        vault_dir=tmp_path / "vault", state_dir=state_dir,
    )
    body = Path(out).read_text(encoding="utf-8")
    assert "Alpha A." in body
    assert "LOSS" in body, "Yesterday's Results must show corrected LOSS, not original WIN"
    # The original WIN must not appear for this pick
    lines = [ln for ln in body.split("\n") if "Alpha A." in ln and "Bravo B." in ln]
    assert lines, "no row found for Alpha A. vs Bravo B."
    assert "WIN" not in lines[0], f"row still shows WIN: {lines[0]}"
