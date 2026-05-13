"""Verify journal-loading sites apply settled_correction overrides."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest


def test_apply_settled_corrections_overrides_outcome_and_pnl():
    from tennis_dry_run import _apply_settled_corrections

    rows = [
        {"type": "open", "pick_id": "p1", "ts": "T0"},
        {"type": "settled", "pick_id": "p1", "ts": "T1", "outcome": "win",
         "pnl": 5.21, "stake": 25.0, "result_winner": "Zverev A.",
         "sxbet_odds": 1.21},
        {"type": "settled_correction", "corrects": "T1", "pick_id": "p1",
         "outcome": "loss", "pnl": -25.0, "stake": 25.0,
         "result_winner": "Darderi L.", "ts": "T2"},
    ]
    out = _apply_settled_corrections(rows)
    # settled_correction rows are removed; settled row keeps shape, gets overridden values.
    assert [r.get("type") for r in out] == ["open", "settled"]
    settled = out[1]
    assert settled["outcome"] == "loss"
    assert settled["pnl"] == -25.0
    assert settled["result_winner"] == "Darderi L."
    assert settled["corrected"] is True


def test_apply_settled_corrections_passes_through_uncorrected_rows():
    from tennis_dry_run import _apply_settled_corrections
    rows = [
        {"type": "open", "pick_id": "p1"},
        {"type": "settled", "pick_id": "p1", "ts": "T1", "outcome": "win", "pnl": 12.5},
    ]
    out = _apply_settled_corrections(rows)
    assert out == rows  # unchanged
    assert "corrected" not in out[1]


def test_apply_settled_corrections_handles_multiple(tmp_path):
    from tennis_dry_run import _apply_settled_corrections
    rows = [
        {"type": "settled", "pick_id": "p1", "ts": "T1", "outcome": "win", "pnl": 5},
        {"type": "settled", "pick_id": "p2", "ts": "T2", "outcome": "loss", "pnl": -25},
        {"type": "settled_correction", "corrects": "T1", "pick_id": "p1", "outcome": "loss", "pnl": -25, "ts": "T3"},
        {"type": "settled_correction", "corrects": "T2", "pick_id": "p2", "outcome": "win", "pnl": 8.39, "ts": "T4"},
    ]
    out = _apply_settled_corrections(rows)
    assert len(out) == 2
    assert all(r["type"] == "settled" for r in out)
    assert out[0]["pnl"] == -25  # p1 was corrected loss
    assert out[1]["pnl"] == 8.39  # p2 was corrected win


def test_replay_three_bankrolls_uses_corrected_pnl_via_load_pipe():
    """When the caller pipes rows through _apply_settled_corrections before
    splitting, replay_three_bankrolls produces the corrected balance."""
    from tennis_dry_run import _apply_settled_corrections
    from tennis_kelly import replay_three_bankrolls

    rows = [
        {"type": "open", "pick_id": "p1", "ts": "2026-05-11T07:00:00+00:00",
         "model_prob": 0.85, "sxbet_odds": 1.21, "sxbet_available_usd": 500.0, "stake": 25.0},
        {"type": "settled", "pick_id": "p1", "ts": "2026-05-12T15:03:00+00:00",
         "outcome": "win", "pnl": 5.21, "stake": 25.0,
         "result_winner": "Zverev A.", "sxbet_odds": 1.21},
        {"type": "settled_correction", "corrects": "2026-05-12T15:03:00+00:00",
         "pick_id": "p1", "outcome": "loss", "pnl": -25.0, "stake": 25.0,
         "result_winner": "Darderi L.", "ts": "2026-05-13T17:00:00+00:00"},
    ]
    effective = _apply_settled_corrections(rows)
    placed = [r for r in effective if r["type"] == "open"]
    settled = [r for r in effective if r["type"] == "settled"]
    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 13))
    # Corrected loss → -25 → balance 475
    assert replay["base"]["balance"] == pytest.approx(475.0)
