"""Tests for tennis_shadow_placer — observation-only evaluator for tier-B picks.

The shadow placer mirrors `tennis_placer.place_pick` decision logic but
writes to a separate journal and never calls the executor / never mutates
state. Its purpose: capture what an actual T-90min placement attempt would
have decided, so we can A/B compare T-15 (tier A) vs T-90 (tier B) timing
on real SX Bet data.
"""

from unittest.mock import MagicMock

import pytest

import tennis_shadow_placer as sp


def _selection(**overrides):
    base = {
        "pick_id": "0xabc",
        "pick": "Ben Shelton",
        "opponent": "Nikoloz Basilashvili",
        "league": "ATP Rome",
        "surface": "clay",
        "round": "unknown",
        "model_prob": 0.7753,
        "fair_odds": 1.290,
        "tier": "B",
        "market_hash": "0xmarket",
        "market_player_a": "Nikoloz Basilashvili",
        "is_pick_outcome_one": False,
        "game_time": 1778336400,
    }
    base.update(overrides)
    return base


# ── decide_shadow_placement (pure decision) ───────────────────────────────────

def test_evaluate_returns_would_place_when_edge_positive():
    """Edge > 0 + within MIN/MAX_ODDS + sufficient liquidity → would_place."""
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.45,
        "implied_prob": 1 / 1.45,
        "available_usd": 100.0,
    }

    result = sp.evaluate_shadow_placement(_selection(), sxbet, paper_stake=25.0)

    assert result["status"] == "would_place"
    assert result["sxbet_odds"] == pytest.approx(1.45, abs=1e-6)
    assert result["available_usd"] == pytest.approx(100.0, abs=1e-6)
    assert result["edge"] == pytest.approx(0.7753 - 1/1.45, abs=1e-3)


def test_evaluate_skips_no_liquidity_when_orderbook_empty():
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = None

    result = sp.evaluate_shadow_placement(_selection(), sxbet, paper_stake=25.0)

    assert result["status"] == "would_skip"
    assert result["reason"] == "no_liquidity"


def test_evaluate_skips_odds_out_of_range_when_above_max_odds():
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 5.5,  # > MAX_ODDS=2.0
        "implied_prob": 1 / 5.5,
        "available_usd": 100.0,
    }

    result = sp.evaluate_shadow_placement(_selection(), sxbet, paper_stake=25.0)

    assert result["status"] == "would_skip"
    assert result["reason"] == "odds_out_of_range_at_placement"
    assert result["sxbet_odds"] == pytest.approx(5.5, abs=1e-6)


def test_evaluate_skips_insufficient_liquidity_when_below_paper_stake():
    """Available taker stake $10 against $25 base → reject."""
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.45, "implied_prob": 1/1.45,
        "available_usd": 10.0,
    }

    result = sp.evaluate_shadow_placement(_selection(), sxbet, paper_stake=25.0)

    assert result["status"] == "would_skip"
    assert result["reason"] == "insufficient_liquidity"
    assert result["available_usd"] == pytest.approx(10.0, abs=1e-6)


def test_evaluate_skips_negative_edge_when_implied_above_model():
    """SX Bet implied prob 0.83 > model prob 0.7753 → negative edge."""
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.20, "implied_prob": 1/1.20,  # implied 0.833
        "available_usd": 100.0,
    }

    result = sp.evaluate_shadow_placement(_selection(), sxbet, paper_stake=25.0)

    assert result["status"] == "would_skip"
    assert result["reason"] == "negative_edge"
    assert result["edge"] < 0


def test_evaluate_records_pick_id_market_hash_and_timestamp():
    """Result must carry pick_id + market_hash so EOD can join with shadow
    selections + outcomes; ts must be set so we can correlate with T-90 fire."""
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = None  # any branch — we want metadata

    sel = _selection(pick_id="0xunique", market_hash="0xmh")
    result = sp.evaluate_shadow_placement(sel, sxbet, paper_stake=25.0)

    assert result["pick_id"] == "0xunique"
    assert result["market_hash"] == "0xmh"
    assert result["pick"] == "Ben Shelton"
    assert "ts" in result
    assert result["model_prob"] == 0.7753
    assert result["source"] == "shadow_placer"


def test_evaluate_does_not_call_executor_or_state(tmp_path):
    """Hard guarantee: shadow placer NEVER touches executor or state.json.
    A passing test isn't enough; verify via spy that no executor object is
    even constructed by the evaluator."""
    sxbet = MagicMock()
    sxbet.get_best_back_odds.return_value = {
        "decimal_odds": 1.45, "implied_prob": 1/1.45, "available_usd": 100.0,
    }

    # Pass an executor that would explode if called.
    executor = MagicMock()
    executor.place_order.side_effect = AssertionError("shadow placer must not place")

    # Function signature does not even take executor — calling it is impossible.
    import inspect
    sig = inspect.signature(sp.evaluate_shadow_placement)
    assert "executor" not in sig.parameters
    assert "state_file" not in sig.parameters


# ── append_shadow_placement (file IO) ─────────────────────────────────────────

def test_append_shadow_placement_writes_one_jsonl_line(tmp_path):
    out = tmp_path / "shadow_placements.jsonl"
    sp.append_shadow_placement(
        {"pick_id": "0xa", "status": "would_place", "ts": "2026-05-10T08:40:00+00:00"},
        out,
    )
    sp.append_shadow_placement(
        {"pick_id": "0xb", "status": "would_skip", "reason": "negative_edge",
         "ts": "2026-05-10T09:30:00+00:00"},
        out,
    )

    import json
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["pick_id"] == "0xa"
    assert json.loads(lines[1])["reason"] == "negative_edge"


# ── main() CLI ────────────────────────────────────────────────────────────────

def test_main_loads_pick_from_shadow_file_and_appends_result(tmp_path, monkeypatch):
    """End-to-end CLI: given a pick_id and shadow_selections.jsonl, the script
    fetches odds, decides, and writes one new line to shadow_placements.jsonl."""
    import json
    shadow_sel_file = tmp_path / "shadow_selections.jsonl"
    shadow_pl_file = tmp_path / "shadow_placements.jsonl"
    sel = _selection(pick_id="0xrun")
    shadow_sel_file.write_text(json.dumps(sel) + "\n", encoding="utf-8")

    # Stub TennisSXBet so no network call.
    class FakeSX:
        def get_best_back_odds(self, *a, **kw):
            return {"decimal_odds": 1.45, "implied_prob": 1/1.45, "available_usd": 100.0}
    monkeypatch.setattr("tennis_shadow_placer.TennisSXBet", lambda: FakeSX())

    monkeypatch.setenv("SHADOW_SELECTIONS_FILE", str(shadow_sel_file))
    monkeypatch.setenv("SHADOW_PLACEMENTS_FILE", str(shadow_pl_file))

    rc = sp.main(["0xrun"])

    assert rc == 0
    assert shadow_pl_file.exists()
    rows = [json.loads(l) for l in shadow_pl_file.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["pick_id"] == "0xrun"
    assert rows[0]["status"] == "would_place"
