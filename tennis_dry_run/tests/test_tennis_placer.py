"""Tests for tennis_placer — late-binding T-15min order placement.

The placer is invoked per-selection by `at` (or directly by the identifier
for imminent matches). It re-fetches the SX Bet orderbook close to match
start, applies the same odds-range filter as the scan loop, and submits
via the executor when the pick passes.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import tennis_placer as tp


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def pending_file(tmp_path):
    p = tmp_path / "pending_selections.jsonl"
    return p


@pytest.fixture
def state_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "balance": 500.0,
        "total_bets": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0,
        "open_picks": {},
        "today_bets": 0,
    }), encoding="utf-8")
    return p


def _selection(pick_id="0xabc"):
    return {
        "pick_id": pick_id,
        "pick": "Aryna Sabalenka",
        "opponent": "Magda Linette",
        "league": "WTA Madrid",
        "surface": "clay",
        "round": "R32",
        "model_prob": 0.85,
        "fair_odds": 1.176,
        "market_hash": pick_id,
        "market_player_a": "Aryna Sabalenka",
        "is_pick_outcome_one": True,
        "game_time": 1746540000,
        "ts": "2026-05-06T07:00:00+00:00",
    }


# ── load_selection ────────────────────────────────────────────────────────────

def test_load_selection_returns_latest_matching_pick_id(pending_file):
    """Two entries for same pick_id → latest one wins."""
    sel1 = _selection("0xabc")
    sel1["model_prob"] = 0.81
    sel2 = _selection("0xabc")
    sel2["model_prob"] = 0.85
    other = _selection("0xdef")
    pending_file.write_text(
        json.dumps(sel1) + "\n" +
        json.dumps(other) + "\n" +
        json.dumps(sel2) + "\n",
        encoding="utf-8",
    )

    result = tp.load_selection(pending_file, "0xabc")

    assert result["model_prob"] == 0.85


def test_load_selection_returns_none_when_pick_id_missing(pending_file):
    pending_file.write_text(json.dumps(_selection("0xdef")) + "\n", encoding="utf-8")
    assert tp.load_selection(pending_file, "0xabc") is None


# ── place_pick ────────────────────────────────────────────────────────────────

def test_place_pick_returns_already_placed_when_in_open_picks(state_file, pending_file):
    """Idempotency: pick already in state.open_picks → exit cleanly without
    fetching odds or invoking executor."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["open_picks"]["0xabc"] = {"pick": "Aryna Sabalenka"}
    state_file.write_text(json.dumps(state), encoding="utf-8")

    sxbet = MagicMock()
    executor = MagicMock()

    result = tp.place_pick("0xabc", pending_file, state_file, sxbet, executor)

    assert result["status"] == "already_placed"
    sxbet.get_best_back_odds.assert_not_called()
    executor.place_order.assert_not_called()


def test_place_pick_skips_when_no_liquidity(state_file, pending_file):
    """Orderbook returns None → log skip, don't call executor."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = None
    executor = MagicMock()

    result = tp.place_pick("0xabc", pending_file, state_file, sxbet, executor)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_liquidity"
    executor.place_order.assert_not_called()


def test_place_pick_skips_when_odds_above_max(state_file, pending_file):
    """Live SX Bet odds at T-15 still above MAX_ODDS=2.00 → skip."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 8.888,
        "implied_prob": 0.1125,
        "available_usd": 100.0,
    }
    executor = MagicMock()

    result = tp.place_pick("0xabc", pending_file, state_file, sxbet, executor)

    assert result["status"] == "skipped"
    assert result["reason"] == "odds_out_of_range_at_placement"
    executor.place_order.assert_not_called()


def test_place_pick_calls_executor_and_updates_state_on_pass(state_file, pending_file):
    """Happy path: fresh odds in range, edge >= 0 → executor called and
    state.open_picks updated with pick_id so scan loop won't double-pick."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.45,
        "implied_prob": 0.6896,
        "available_usd": 100.0,
    }
    executor = MagicMock()
    executor.place_order.return_value = MagicMock(
        status="dry_run_recorded",
        mode="dry_run",
        trade_entry={
            "type": "open", "mode": "dry_run", "pick_id": "0xabc",
            "pick": "Aryna Sabalenka",
        },
        block_reason=None,
    )

    result = tp.place_pick("0xabc", pending_file, state_file, sxbet, executor)

    assert result["status"] == "placed"
    executor.place_order.assert_called_once()
    pick_context = executor.place_order.call_args[0][0]
    assert pick_context["pick_id"] == "0xabc"
    assert pick_context["sxbet_odds"] == 1.45
    assert pick_context["model_prob"] == 0.85

    # State.open_picks now contains the new entry
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "0xabc" in state["open_picks"]
    assert state["open_picks"]["0xabc"]["pick"] == "Aryna Sabalenka"


def test_place_pick_appends_trade_entry_to_journal_on_pass(tmp_path, state_file, pending_file):
    """Successful placement is appended to trades.jsonl so the EOD report
    sees placer-placed picks (matching the scan-loop's journal pattern)."""
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
    assert rows[0]["pick_id"] == "0xabc"
    assert rows[0]["type"] == "open"


def test_place_pick_skips_when_edge_negative(state_file, pending_file):
    """Live SX Bet odds in range but worse than fair → skip on negative edge."""
    pending_file.write_text(json.dumps(_selection("0xabc")) + "\n", encoding="utf-8")
    sxbet = MagicMock()
    # Implied prob 0.90 > model_prob 0.85 → edge negative
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.11,
        "implied_prob": 0.90,
        "available_usd": 100.0,
    }
    executor = MagicMock()

    result = tp.place_pick("0xabc", pending_file, state_file, sxbet, executor)

    assert result["status"] == "skipped"
    assert result["reason"] == "negative_edge"
    executor.place_order.assert_not_called()
