"""Settle-time invariants — state.balance must equal Σ(journal pnls) exactly."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


def test_state_balance_equals_journal_sum_after_settle(tmp_path, monkeypatch):
    """After run_settle, state.balance — STARTING_BALANCE must equal the
    sum of pnls from the journal, to the cent."""
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")

    state = {
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {
            "p1": {"pick_id": "p1", "pick": "Alexander Zverev",
                   "opponent": "Luciano Darderi", "stake": 25.0,
                   "sxbet_odds": 1.3333333333, "mode": "dry_run",
                   "is_pick_outcome_one": True, "model_prob": 0.85},
        },
    }

    fake_results = [{
        "player_a": "Zverev A.", "player_b": "Darderi L.",
        "winner": "Zverev A.", "tournament": "Rome",
    }]

    class _Exec:
        def reconcile_pick(self, pick, won):
            stake = pick["stake"]
            odds = pick["sxbet_odds"]
            pnl = stake * (odds - 1.0) if won else -stake
            return {"outcome": "win" if won else "loss", "pnl": pnl, "mode": "dry_run"}

    with patch.object(tdr, "scrape_completed_results", return_value=fake_results):
        new_state, _settled_ids = tdr.run_settle(state, _Exec())

    journal_rows = [json.loads(ln) for ln in (tmp_path / "trades.jsonl").read_text().splitlines() if ln.strip()]
    journal_sum = sum(r["pnl"] for r in journal_rows if r.get("type") == "settled")

    assert new_state["balance"] == round(500.0 + journal_sum, 2), \
        f"state.balance {new_state['balance']} drifted from journal sum {round(500.0 + journal_sum, 2)}"
    assert new_state["total_pnl"] == round(journal_sum, 2)


def test_retired_match_settles_to_zero_pnl(tmp_path, monkeypatch):
    import tennis_dry_run as tdr
    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")

    state = {
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {
            "p1": {"pick_id": "p1", "pick": "Alexander Zverev",
                   "opponent": "Luciano Darderi", "stake": 25.0,
                   "sxbet_odds": 1.5, "mode": "dry_run",
                   "is_pick_outcome_one": True, "model_prob": 0.85},
        },
    }
    fake = [{"player_a": "Zverev A.", "player_b": "Darderi L.",
             "winner": "Zverev A.", "tournament": "Rome", "retired": True}]

    class _Exec:
        def reconcile_pick(self, pick, won):
            return {"outcome": "win" if won else "loss",
                    "pnl": 25.0 * 0.5 if won else -25.0, "mode": "dry_run"}

    with patch.object(tdr, "scrape_completed_results", return_value=fake):
        new_state, _settled_ids = tdr.run_settle(state, _Exec())

    assert new_state["balance"] == 500.0  # no change
    assert new_state["wins"] == 0
    assert new_state["losses"] == 0
    assert "p1" not in new_state["open_picks"]
    rows = [json.loads(ln) for ln in (tmp_path / "trades.jsonl").read_text().splitlines() if ln.strip()]
    settled = [r for r in rows if r.get("type") == "settled"]
    assert len(settled) == 1
    assert settled[0]["outcome"] == "retired"
    assert settled[0]["pnl"] == 0.0


def test_settle_writes_audit_entry_to_settlements_jsonl_with_settle_pass_fields(tmp_path, monkeypatch):
    """Per-settle audit row uses settle_pass_* field names and lands in settlements.jsonl."""
    import json
    from unittest.mock import patch

    import tennis_dry_run as tdr
    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")

    state = {
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {
            "p1": {"pick_id": "p1", "pick": "Alpha A.", "opponent": "Bravo B.",
                   "stake": 25.0, "sxbet_odds": 1.5, "mode": "dry_run",
                   "is_pick_outcome_one": True, "model_prob": 0.85},
        },
    }
    fake = [{"player_a": "Alpha A.", "player_b": "Bravo B.",
             "winner": "Alpha A.", "tournament": "Rome", "retired": False}]

    class _Exec:
        def reconcile_pick(self, pick, won):
            return {"outcome": "win" if won else "loss",
                    "pnl": 25.0 * 0.5 if won else -25.0, "mode": "dry_run"}

    with patch.object(tdr, "scrape_completed_results", return_value=fake):
        new_state, settled_ids = tdr.run_settle(state, _Exec())

    audit_path = tmp_path / "settlements.jsonl"
    assert audit_path.exists(), "audit log should be written to settlements.jsonl"

    rows = [json.loads(ln) for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    entry = rows[0]
    assert entry["settle_pass_count"] == 1
    assert entry["settle_pass_wins"] == 1
    assert entry["settle_pass_losses"] == 0
    assert entry["settle_pass_retired"] == 0
    assert entry["settle_pass_pnl"] == 12.5  # 25 * 0.5
    # Old field names are gone.
    assert "daily_pnl" not in entry
    assert "daily_wins" not in entry
