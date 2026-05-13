"""Tests for tennis_eod_report — daily EOD performance summary.

Run by cron at 22:00 UTC. Reads state.json + trades.jsonl + skipped.jsonl,
uses portfolio renderers to build an EOD Performance section, and appends
(or replaces) it in <vault_dir>/YYYY-MM-DD.md. Also re-renders the rolling
Tennis-Dry-Run-Report.md via write_eod_report.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import tennis_eod_report as eod


# ── Fixtures ──────────────────────────────────────────────────────────────────

TODAY_DT = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
TODAY = TODAY_DT.date()
YESTERDAY = TODAY - timedelta(days=1)


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ── write_eod_report — integration ───────────────────────────────────────────

def test_eod_writes_portfolio_and_kelly_pnl_columns(tmp_path):
    """EOD section must include Portfolio block + Today's Placer Activity + Today's Settlements with Kelly columns."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        '{"balance": 500.0, "open_picks": {}, "today_bets": 0, "today_date": "2026-05-08"}'
    )
    (state_dir / "trades.jsonl").write_text(
        '{"type":"open","pick_id":"0xdjk","pick":"Novak Djokovic","opponent":"Dino Prizmic","league":"ATP Rome","surface":"clay","model_prob":0.8713,"sxbet_odds":1.4953,"sxbet_available_usd":1000.0,"stake":25.0,"ts":"2026-05-08T11:55:00+00:00","mode":"dry_run","edge":0.2026}\n'
        '{"type":"settled","pick_id":"0xdjk","pick":"Novak Djokovic","opponent":"Dino Prizmic","outcome":"win","pnl":12.38,"ts":"2026-05-08T15:00:00+00:00"}\n'
    )
    (state_dir / "skipped.jsonl").write_text(
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xzv","pick":"Alexander Zverev","opponent":"Daniel Altmaier","league":"ATP Rome","sxbet_odds":1.087,"edge":-0.063,"ts":"2026-05-08T10:45:00+00:00"}\n'
    )
    (state_dir / "pending_selections.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    daily_path = vault_dir / "2026-05-08.md"
    daily_path.write_text("# Tennis Daily Report — 2026-05-08\n\n_existing BOD content_\n", encoding="utf-8")
    rolling_path = tmp_path / "rolling.md"

    out = write_eod_report(
        now_utc=datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
        rolling_path=rolling_path,
    )

    body = out.read_text(encoding="utf-8")
    # BOD content preserved
    assert "_existing BOD content_" in body
    # EOD section appended
    assert "## EOD Performance — 2026-05-08" in body
    assert "### Portfolio (snapshot 2026-05-08 22:00 UTC)" in body
    assert "## Today's Placer Activity" in body
    assert "Djokovic" in body
    assert "Zverev" in body
    assert "skipped: negative_edge" in body
    assert "## Today's Settlements" in body
    assert "WIN" in body
    assert "$+12.38" in body  # base
    assert "$+37." in body    # quarter-K
    assert "$+75." in body    # half-K
    # Rolling file re-rendered
    rolling = rolling_path.read_text(encoding="utf-8")
    assert "## Tennis Dry Run Report" in rolling
    assert "### Performance (cumulative)" in rolling
    assert "### Backtest vs Dry Run" in rolling


def test_eod_creates_stub_when_no_morning_report(tmp_path):
    """If the daily file doesn't exist, write_eod_report creates a stub + EOD section."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        '{"balance": 500.0, "open_picks": {}}'
    )
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "## EOD Performance — 2026-05-08" in body
    assert "type: tennis-daily-report" in body


def test_eod_includes_stale_carryover_section(tmp_path):
    """EOD must surface today's picks that never settled (placer-skipped or
    no-attempt). Otherwise these picks vanish from the report entirely."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    # Today (May 8) had 2 picks: both skipped at placement, neither settled.
    skipped_today_iso = "2026-05-08T10:45:00+00:00"
    (state_dir / "skipped.jsonl").write_text(
        '{"type":"skipped","source":"placer","reason":"negative_edge",'
        '"pick_id":"0xzv","pick":"Alexander Zverev","opponent":"Daniel Altmaier",'
        '"league":"ATP Rome","sxbet_odds":1.087,"edge":-0.063,'
        f'"ts":"{skipped_today_iso}"' + "}\n"
        '{"type":"skipped","source":"placer","reason":"odds_out_of_range_at_placement",'
        '"pick_id":"0xsw","pick":"Iga Swiatek","opponent":"Catherine McNally",'
        '"league":"WTA Rome","sxbet_odds":16.0,'
        '"ts":"2026-05-08T08:45:00+00:00"}\n'
    )

    # Pending file: 2 picks for today + 1 for yesterday (already past, must NOT
    # appear in today's carryover) + 1 for tomorrow (future, must NOT appear).
    import json
    from datetime import datetime, timezone
    def _ts_unix(y, m, d, h, mi=0):
        return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())
    pending_rows = [
        {"pick_id": "0xzv", "pick": "Alexander Zverev", "opponent": "Daniel Altmaier",
         "league": "ATP Rome", "game_time": _ts_unix(2026, 5, 8, 11, 0)},
        {"pick_id": "0xsw", "pick": "Iga Swiatek", "opponent": "Catherine McNally",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 8, 9, 0)},
        {"pick_id": "0xyest", "pick": "Yesterday Pick", "opponent": "X",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 7, 14, 0)},
        {"pick_id": "0xtmw", "pick": "Tomorrow Pick", "opponent": "Y",
         "league": "WTA Rome", "game_time": _ts_unix(2026, 5, 9, 14, 0)},
    ]
    (state_dir / "pending_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in pending_rows) + "\n"
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Stale Carryovers" in body
    assert "Alexander Zverev" in body
    assert "negative_edge" in body
    assert "1.087" in body
    assert "Iga Swiatek" in body
    assert "odds_out_of_range_at_placement" in body
    assert "Yesterday Pick" not in body
    assert "Tomorrow Pick" not in body


# ── resolve_shadow_outcomes ───────────────────────────────────────────────────

def test_resolve_shadow_outcomes_marks_won_when_pick_won():
    """Shadow pick whose name matches the recorded winner → status='WIN',
    theoretical_pnl = stake * (fair_odds - 1)."""
    from tennis_eod_report import resolve_shadow_outcomes

    shadows = [{
        "pick_id": "0xa", "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
        "league": "ATP Rome", "fair_odds": 1.290, "model_prob": 0.7753,
        "game_time": 1778336400,  # 2026-05-09 14:20 UTC
    }]
    completed = [{
        "player_a": "Shelton B.", "player_b": "Basilashvili N.",
        "winner": "Shelton B.", "tournament": "Rome",
    }]

    out = resolve_shadow_outcomes(shadows, completed, base_stake_usd=25.0)

    assert len(out) == 1
    row = out[0]
    assert row["status"] == "WIN"
    assert row["theoretical_pnl"] == pytest.approx(25.0 * 0.290, abs=1e-6)
    assert row["result_winner"] == "Shelton B."


def test_resolve_shadow_outcomes_marks_loss_when_opponent_won():
    from tennis_eod_report import resolve_shadow_outcomes

    shadows = [{
        "pick_id": "0xb", "pick": "Aryna Sabalenka", "opponent": "Magda Linette",
        "league": "WTA Rome", "fair_odds": 1.40, "model_prob": 0.71,
        "game_time": 1778336400,
    }]
    completed = [{
        "player_a": "Sabalenka A.", "player_b": "Linette M.",
        "winner": "Linette M.", "tournament": "Rome",
    }]

    out = resolve_shadow_outcomes(shadows, completed, base_stake_usd=25.0)

    assert out[0]["status"] == "LOSS"
    assert out[0]["theoretical_pnl"] == pytest.approx(-25.0, abs=1e-6)
    assert out[0]["result_winner"] == "Linette M."


def test_resolve_shadow_outcomes_marks_pending_when_no_match_found():
    """Match hasn't completed (or no result on TennisExplorer) → pending,
    no PnL."""
    from tennis_eod_report import resolve_shadow_outcomes

    shadows = [{
        "pick_id": "0xc", "pick": "Future Pick", "opponent": "Future Opp",
        "league": "ATP", "fair_odds": 1.30, "model_prob": 0.77,
        "game_time": 1778500000,
    }]
    completed = [
        {"player_a": "Other A", "player_b": "Other B", "winner": "Other A"},
    ]

    out = resolve_shadow_outcomes(shadows, completed, base_stake_usd=25.0)

    assert out[0]["status"] == "pending"
    assert out[0]["theoretical_pnl"] == 0.0
    assert out[0].get("result_winner") is None


def test_resolve_shadow_outcomes_preserves_input_fields():
    """Resolution adds outcome fields without dropping any input keys."""
    from tennis_eod_report import resolve_shadow_outcomes

    shadows = [{
        "pick_id": "0xd", "pick": "Jannik Sinner", "opponent": "Sebastian Ofner",
        "league": "ATP", "fair_odds": 1.30, "model_prob": 0.77,
        "game_time": 1778336400, "tier": "B", "extra_key": "preserved",
    }]
    completed = [{"player_a": "Sinner J.", "player_b": "Ofner S.",
                  "winner": "Sinner J."}]

    out = resolve_shadow_outcomes(shadows, completed)

    assert out[0]["extra_key"] == "preserved"
    assert out[0]["tier"] == "B"
    assert out[0]["pick_id"] == "0xd"
    assert out[0]["status"] == "WIN"


def test_eod_includes_shadow_picks_section(tmp_path, monkeypatch):
    """EOD must surface today's tier-B shadow picks. With monkeypatched
    scrape_completed_results returning empty, all picks resolve to pending
    (no real network call from this test)."""
    from tennis_eod_report import write_eod_report
    from datetime import datetime, timezone

    monkeypatch.setattr("tennis_dry_run.scrape_completed_results",
                        lambda target_date=None: [])

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    # 2 shadow picks today + 1 from yesterday (must NOT appear).
    import json
    def _ts_unix(y, m, d, h, mi=0):
        return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())
    shadow_rows = [
        {"pick_id": "0xs1", "pick": "Today Shadow A", "opponent": "X",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 14, 0)},
        {"pick_id": "0xs2", "pick": "Today Shadow B", "opponent": "Y",
         "league": "WTA Rome", "surface": "clay",
         "model_prob": 0.78, "fair_odds": 1.282, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 16, 0)},
        {"pick_id": "0xs3", "pick": "Yesterday Shadow", "opponent": "Z",
         "league": "WTA Rome", "surface": "clay",
         "model_prob": 0.75, "fair_odds": 1.333, "tier": "B",
         "game_time": _ts_unix(2026, 5, 8, 14, 0)},
    ]
    (state_dir / "shadow_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow_rows) + "\n"
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 9, 22, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Shadow Picks (tier B, 70-80% — not placed)" in body
    assert "Today Shadow A" in body
    assert "Today Shadow B" in body
    assert "1.351" in body
    assert "Yesterday Shadow" not in body  # filtered to today only


def test_eod_resolves_shadow_outcomes_and_renders_aggregate(tmp_path, monkeypatch):
    """End-to-end: shadow picks for today are paired with TennisExplorer
    results, marked WIN/LOSS, and aggregated into win-rate + theoretical PnL."""
    from tennis_eod_report import write_eod_report
    from datetime import datetime, timezone
    import json

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    def _ts_unix(y, m, d, h, mi=0):
        return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())
    shadow_rows = [
        {"pick_id": "0xs1", "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 14, 0)},
        {"pick_id": "0xs2", "pick": "Aryna Sabalenka", "opponent": "Magda Linette",
         "league": "WTA Rome", "surface": "clay",
         "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 16, 0)},
    ]
    (state_dir / "shadow_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow_rows) + "\n"
    )

    # Stub TennisExplorer: Shelton wins, Sabalenka loses to Linette.
    # EOD passes today=2026-05-09 as target_date.
    captured_dates: list = []
    def _stub_scrape(target_date=None):
        captured_dates.append(target_date)
        return [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
            {"player_a": "Sabalenka A.", "player_b": "Linette M.",
             "winner": "Linette M.", "tournament": "Rome"},
        ]
    monkeypatch.setattr("tennis_dry_run.scrape_completed_results", _stub_scrape)

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 9, 22, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "Outcome" in body
    assert "WIN" in body
    assert "LOSS" in body
    assert "Resolved: 2" in body
    assert "Wins: 1" in body
    assert "Win rate: 50.0%" in body
    # PnL: Shelton WIN $25*(1.29-1)=$7.25; Sabalenka LOSS $-25.00 → −$17.75
    assert "Theoretical PnL: $-17.75" in body
    # Regression: scraper must be called with the today UTC date so TE's
    # Prague-time rollover doesn't return tomorrow's empty schedule.
    from datetime import date
    assert captured_dates == [date(2026, 5, 9)]


def test_eod_merges_shadow_placements_into_shadow_section(tmp_path, monkeypatch):
    """If shadow_placements.jsonl has a record for a tier-B pick (T-90 shadow
    fire), the rendered Shadow Picks section must show the T-90 result column."""
    from tennis_eod_report import write_eod_report
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr("tennis_dry_run.scrape_completed_results",
                        lambda target_date=None: [])

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    def _ts_unix(y, m, d, h, mi=0):
        return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())
    shadow_rows = [{
        "pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Basilashvili",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
        "game_time": _ts_unix(2026, 5, 10, 10, 10),
    }]
    (state_dir / "shadow_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow_rows) + "\n"
    )

    placements = [{
        "source": "shadow_placer", "pick_id": "0xshelt",
        "pick": "Ben Shelton", "status": "would_place",
        "sxbet_odds": 3.31, "available_usd": 284.5, "edge": 0.4753,
        "ts": "2026-05-10T08:40:00+00:00",
    }]
    (state_dir / "shadow_placements.jsonl").write_text(
        "\n".join(json.dumps(r) for r in placements) + "\n"
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 10, 22, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "T-90" in body
    assert "would_place" in body
    assert "3.310" in body
    assert "1/1 would have placed" in body


def test_eod_includes_7day_placer_rejection_diagnostics(tmp_path):
    """EOD must include the rolling 7-day placer outcome distribution so we
    can see at a glance whether negative_edge / odds_out_of_range gates are
    starving signal capture."""
    from tennis_eod_report import write_eod_report
    from datetime import datetime, timezone

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    # 1 placed trade in window (full shape required by tennis_kelly replay)
    (state_dir / "trades.jsonl").write_text(
        '{"type":"open","pick_id":"0xdjk","pick":"Djokovic","opponent":"Prizmic",'
        '"league":"ATP Rome","surface":"clay","model_prob":0.87,'
        '"sxbet_odds":1.5,"sxbet_available_usd":1000.0,"edge":0.20,"stake":25.0,'
        '"ts":"2026-05-08T11:55:00+00:00","mode":"dry_run"}\n'
    )
    # 6 placer skips in window + 1 outside window
    (state_dir / "skipped.jsonl").write_text(
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xa","ts":"2026-05-09T08:45:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xb","ts":"2026-05-09T12:15:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xc","ts":"2026-05-08T10:45:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"odds_out_of_range_at_placement","pick_id":"0xd","ts":"2026-05-08T08:45:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"odds_out_of_range_at_placement","pick_id":"0xe","ts":"2026-05-07T10:15:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"no_liquidity","pick_id":"0xf","ts":"2026-05-07T08:45:00+00:00"}\n'
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xold","ts":"2026-04-01T08:45:00+00:00"}\n'
    )
    (state_dir / "pending_selections.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 9, 22, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Placer Rejection Diagnostics (last 7 days)" in body
    # 7 in-window attempts (6 skips + 1 placed); old skip excluded
    assert "**7**" in body
    assert "negative_edge" in body
    assert "odds_out_of_range_at_placement" in body
    assert "no_liquidity" in body
    assert "placed" in body


def test_eod_idempotent_on_rerun(tmp_path):
    """Running write_eod_report twice keeps exactly one EOD section."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now_utc = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now_utc, state_dir=state_dir, vault_dir=vault_dir)
    write_eod_report(now_utc=now_utc, state_dir=state_dir, vault_dir=vault_dir)

    body = (vault_dir / "2026-05-08.md").read_text(encoding="utf-8")
    assert body.count("## EOD Performance — 2026-05-08") == 1
