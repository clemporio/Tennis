"""Tests for the persistent shadow_outcomes.jsonl append-only log written
by tennis_eod_report.write_eod_report after resolve_shadow_outcomes.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _ts_unix(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_eod_appends_resolved_shadow_outcomes_to_jsonl(tmp_path, monkeypatch):
    """After resolve_shadow_outcomes, write_eod_report must append each
    resolved row (status in {WIN, LOSS, RETIRED}) to shadow_outcomes.jsonl
    with a resolved_at timestamp. Pending rows are NOT appended."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    shadow_rows = [
        {"pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 14, 0)},
        {"pick_id": "0xsab", "pick": "Aryna Sabalenka", "opponent": "Magda Linette",
         "league": "WTA Rome", "surface": "clay",
         "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 16, 0)},
        {"pick_id": "0xpending", "pick": "Future Player", "opponent": "X",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.75, "fair_odds": 1.333, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 20, 0)},
    ]
    (state_dir / "shadow_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow_rows) + "\n"
    )

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
            {"player_a": "Sabalenka A.", "player_b": "Linette M.",
             "winner": "Linette M.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    log_path = state_dir / "shadow_outcomes.jsonl"
    assert log_path.exists()
    rows = _read_jsonl(log_path)
    assert {r["pick_id"] for r in rows} == {"0xshelt", "0xsab"}
    by_id = {r["pick_id"]: r for r in rows}
    assert by_id["0xshelt"]["status"] == "WIN"
    assert by_id["0xshelt"]["theoretical_pnl"] == pytest.approx(7.25)
    assert by_id["0xshelt"]["result_winner"] == "Shelton B."
    assert by_id["0xsab"]["status"] == "LOSS"
    assert by_id["0xsab"]["theoretical_pnl"] == pytest.approx(-25.0)
    assert by_id["0xshelt"]["resolved_at"] == "2026-05-09T22:00:00+00:00"
    assert by_id["0xshelt"]["model_prob"] == pytest.approx(0.7753)
    assert by_id["0xshelt"]["fair_odds"] == pytest.approx(1.290)
    assert by_id["0xshelt"]["tier"] == "B"


def test_eod_shadow_log_is_idempotent_on_rerun(tmp_path, monkeypatch):
    """Running EOD twice with identical outcomes appends each row only once.
    Dedup key: (pick_id, status)."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")
    (state_dir / "shadow_selections.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
        "game_time": _ts_unix(2026, 5, 9, 14, 0),
    }) + "\n")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)
    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    rows = _read_jsonl(state_dir / "shadow_outcomes.jsonl")
    assert len(rows) == 1
    assert rows[0]["pick_id"] == "0xshelt"


def test_eod_shadow_log_appends_when_pending_becomes_resolved(tmp_path, monkeypatch):
    """If a pick was pending yesterday and resolves today, a new row is
    appended for the resolved status."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    (state_dir / "shadow_outcomes.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "status": "pending",
        "theoretical_pnl": 0.0, "result_winner": None,
        "resolved_at": "2026-05-08T22:00:00+00:00",
    }) + "\n")

    (state_dir / "shadow_selections.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Nikoloz Basilashvili",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
        "game_time": _ts_unix(2026, 5, 9, 14, 0),
    }) + "\n")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    rows = _read_jsonl(state_dir / "shadow_outcomes.jsonl")
    assert len(rows) == 2
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["WIN", "pending"]


def test_eod_writes_no_log_when_no_shadow_picks(tmp_path, monkeypatch):
    """If there are no shadow selections, the log file is NOT created. (No
    silent empty-file pollution.)"""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")
    (state_dir / "shadow_selections.jsonl").write_text("")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    log_path = state_dir / "shadow_outcomes.jsonl"
    assert not log_path.exists()
