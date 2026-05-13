"""Placer must tag journal open rows with source='identifier_placer' so a
post-hoc audit can attribute every pick to its originating pipeline (Task G).

After run_scan retirement (Task H), the legacy 'run_scan_legacy' tag is
gone too — but until then, both sources appear in the journal and must be
distinguishable.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock

import tennis_placer as tp


def _selection(market_hash: str) -> dict:
    return {
        "type": "selection", "tier": "A",
        "pick_id": market_hash, "market_hash": market_hash,
        "market_player_a": "Aryna Sabalenka", "pick": "Aryna Sabalenka",
        "opponent": "Coco Gauff", "league": "WTA Madrid",
        "surface": "clay", "round": "QF",
        "model_prob": 0.85, "fair_odds": 1.176,
        "sxbet_odds": 1.5, "sxbet_available_usd": 100.0,
        "edge": 0.18, "is_pick_outcome_one": True,
        "game_time": 1715000000,
        "ts": "2026-05-07T07:00:00+00:00",
    }


@pytest.fixture
def state_file(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({
        "balance": 500.0, "today_bets": 0, "today_date": "2026-05-07",
        "open_picks": {},
    }), encoding="utf-8")
    return sf


@pytest.fixture
def pending_file(tmp_path):
    return tmp_path / "pending_selections.jsonl"


def test_placer_writes_source_identifier_placer(tmp_path, state_file, pending_file):
    """After place_pick completes successfully, the appended journal open row
    must include source='identifier_placer'."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    trades_file = tmp_path / "trades.jsonl"

    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.45, "implied_prob": 0.6896, "available_usd": 100.0,
    }
    executor = MagicMock()
    trade_entry = {
        "type": "open", "mode": "dry_run", "pick_id": "0xabc",
        "pick": "Aryna Sabalenka", "ts": "2026-05-07T08:45:00+00:00",
    }
    executor.place_order.return_value = MagicMock(
        status="dry_run_recorded", mode="dry_run",
        trade_entry=trade_entry, block_reason=None,
    )

    result = tp.place_pick("0xabc", pending_file, state_file,
                           sxbet, executor, trades_file=trades_file)

    assert result["status"] == "placed"
    rows = [
        json.loads(line)
        for line in trades_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["type"] == "open"
    assert rows[0].get("source") == "identifier_placer", \
        f"journal open row missing source field: {rows[0]}"


def test_run_scan_writes_source_run_scan_legacy(tmp_path, monkeypatch):
    """run_scan's journal open row must include source='run_scan_legacy'
    (until Task H retires run_scan, then the function is a no-op and this
    test is deleted alongside test_run_scan_excludes_challengers.py)."""
    import tennis_dry_run as tdr
    import tennis_sxbet

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")
    monkeypatch.setattr(tdr, "SKIPPED_FILE", tmp_path / "skipped.jsonl")
    monkeypatch.setattr(tdr, "ELO_FILE", tmp_path / "nonexistent_elo.json")

    class _Sxbet:
        def get_all_tennis_markets(self):
            return [{
                "market_hash": "0xabc" + "00" * 30,
                "player_a": "Player A", "player_b": "Player B",
                "league": "ATP Rome",
            }]
        def get_best_back_odds(self, mh, pick_name, outcome_one_name):
            return {"decimal_odds": 1.50, "available_usd": 200.0}
    monkeypatch.setattr(tennis_sxbet, "TennisSXBet", _Sxbet)
    monkeypatch.setattr(tdr, "scrape_scheduled_matches", lambda: [])
    monkeypatch.setattr(tdr, "_find_player_elo", lambda name, elo_data: {"overall": 1700.0})
    monkeypatch.setattr(tdr, "_build_model_input", lambda elo_entry, surface: {"pa_elo": 1700.0})

    class _Predictor:
        MIN_CONFIDENCE = 0.0
        def load(self): return True
        def predict_match(self, **kw): return {"prob_a": 0.9, "prob_b": 0.1}
    monkeypatch.setattr(tdr, "TennisModelPredictor", _Predictor)

    class _Exec:
        def place_order(self, pick):
            return MagicMock(
                status="dry_run_recorded", mode="dry_run", block_reason=None,
                trade_entry={"type": "open", "pick_id": pick["pick_id"],
                             "pick": pick["pick"], "stake": 25.0,
                             "sxbet_odds": pick["sxbet_odds"],
                             "ts": pick["ts"]},
            )

    state = {"balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
             "open_picks": {}, "today_bets": 0, "today_date": "2026-05-14"}
    tdr.run_scan(state, _Exec())

    journal_path = tmp_path / "trades.jsonl"
    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    open_rows = [r for r in rows if r.get("type") == "open"]
    assert len(open_rows) == 1, f"expected 1 open row, got {rows}"
    assert open_rows[0].get("source") == "run_scan_legacy", \
        f"run_scan open row missing source field: {open_rows[0]}"
