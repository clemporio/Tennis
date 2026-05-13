"""Placer tags journal open rows with source='identifier_placer' so a
post-hoc audit can attribute every pick to its originating pipeline (Task G)."""

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


