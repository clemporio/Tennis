"""Tests for tools/correct_settlements.py — Zverev/Rublev journal correction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.correct_settlements import correct_journal, recompute_state


ZVEREV_OPEN = {
    "type": "open", "mode": "dry_run",
    "pick_id": "0x7a63f5c4b1c0d4b810701e710640a3422dc051fefd294a6fd99305cec092e490",
    "pick": "Alexander Zverev", "opponent": "Luciano Darderi",
    "league": "ATP Rome", "stake": 25.0,
    "sxbet_odds": 1.2084592145015105,
    "ts": "2026-05-11T07:00:40.428935+00:00",
}
ZVEREV_WRONG_SETTLED = {
    "type": "settled",
    "pick_id": "0x7a63f5c4b1c0d4b810701e710640a3422dc051fefd294a6fd99305cec092e490",
    "pick": "Alexander Zverev", "opponent": "Luciano Darderi",
    "outcome": "win", "pnl": 5.21, "sxbet_odds": 1.2084592145015105,
    "stake": 25.0, "balance": 497.43, "total_pnl": -2.57,
    "result_winner": "Zverev A.", "tournament": "Rome", "mode": "dry_run",
    "ts": "2026-05-12T15:03:25.184632+00:00",
}
RUBLEV_OPEN = {
    "type": "open", "mode": "dry_run",
    "pick_id": "0xf24dcf86a45a5451c444d8f460b29efd6f93e2697d23d45e3c890bfe0c0e1042",
    "pick": "Andrey Rublev", "opponent": "Nikoloz Basilashvili",
    "league": "ATP Rome", "stake": 25.0,
    "sxbet_odds": 1.335559265442404,
    "ts": "2026-05-12T07:00:37.203725+00:00",
}
RUBLEV_WRONG_SETTLED = {
    "type": "settled",
    "pick_id": "0xf24dcf86a45a5451c444d8f460b29efd6f93e2697d23d45e3c890bfe0c0e1042",
    "pick": "Andrey Rublev", "opponent": "Nikoloz Basilashvili",
    "outcome": "loss", "pnl": -25.0, "sxbet_odds": 1.335559265442404,
    "stake": 25.0, "balance": 472.43, "total_pnl": -27.57,
    "result_winner": "Basilashvili N.", "tournament": "Rome", "mode": "dry_run",
    "ts": "2026-05-12T18:03:28.787451+00:00",
}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_correct_journal_appends_two_corrections(tmp_path):
    journal = tmp_path / "trades.jsonl"
    _write_jsonl(journal, [ZVEREV_OPEN, RUBLEV_OPEN, ZVEREV_WRONG_SETTLED, RUBLEV_WRONG_SETTLED])

    n_appended = correct_journal(journal)
    assert n_appended == 2

    rows = _read_jsonl(journal)
    corrections = [r for r in rows if r.get("type") == "settled_correction"]
    assert len(corrections) == 2

    z = next(c for c in corrections if c["pick"] == "Alexander Zverev")
    assert z["outcome"] == "loss"
    assert z["pnl"] == pytest.approx(-25.0)
    assert z["corrects"] == ZVEREV_WRONG_SETTLED["ts"]

    r = next(c for c in corrections if c["pick"] == "Andrey Rublev")
    assert r["outcome"] == "win"
    expected_pnl = 25.0 * (1.335559265442404 - 1.0)
    assert r["pnl"] == pytest.approx(expected_pnl, abs=0.01)
    assert r["corrects"] == RUBLEV_WRONG_SETTLED["ts"]


def test_correct_journal_is_idempotent(tmp_path):
    journal = tmp_path / "trades.jsonl"
    _write_jsonl(journal, [ZVEREV_OPEN, RUBLEV_OPEN, ZVEREV_WRONG_SETTLED, RUBLEV_WRONG_SETTLED])

    correct_journal(journal)
    n_second = correct_journal(journal)
    assert n_second == 0
    rows = _read_jsonl(journal)
    assert sum(1 for r in rows if r.get("type") == "settled_correction") == 2


def test_recompute_state_after_correction(tmp_path):
    journal = tmp_path / "trades.jsonl"
    state_path = tmp_path / "state.json"
    _write_jsonl(journal, [ZVEREV_OPEN, RUBLEV_OPEN, ZVEREV_WRONG_SETTLED, RUBLEV_WRONG_SETTLED])
    state_path.write_text(json.dumps({
        "balance": 472.4346, "total_pnl": -27.5654,
        "wins": 3, "losses": 2, "total_bets": 6,
        "open_picks": {}, "today_bets": 0, "today_date": "2026-05-13",
    }), encoding="utf-8")

    correct_journal(journal)
    new_state = recompute_state(journal, state_path, starting_balance=500.0)

    assert new_state["balance"] == pytest.approx(475.61, abs=0.01)
    assert new_state["total_pnl"] == pytest.approx(-24.39, abs=0.01)
    assert new_state["wins"] == 3
    assert new_state["losses"] == 2
