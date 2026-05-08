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
        '{"type":"settled","pick_id":"0xdjk","pick":"Novak Djokovic","opponent":"Dino Prizmic","won":true,"pnl":12.38,"ts":"2026-05-08T15:00:00+00:00"}\n'
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
