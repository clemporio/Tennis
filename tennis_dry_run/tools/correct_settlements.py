"""Append correction-settlement entries for known-wrong 2026-05-12 picks.

Two settlements were inverted by the tiebreak-superscript scraper bug
(now fixed). This script appends `type: settled_correction` rows that
encode the right outcome, references the wrong row by its `ts`, and
recomputes `state.json` to match.

Idempotent: re-running detects existing corrections by `corrects` field
and appends nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

WRONG_SETTLES = [
    {
        "pick_id": "0x7a63f5c4b1c0d4b810701e710640a3422dc051fefd294a6fd99305cec092e490",
        "wrong_ts": "2026-05-12T15:03:25.184632+00:00",
        "correct_outcome": "loss",
        "actual_winner": "Darderi L.",
    },
    {
        "pick_id": "0xf24dcf86a45a5451c444d8f460b29efd6f93e2697d23d45e3c890bfe0c0e1042",
        "wrong_ts": "2026-05-12T18:03:28.787451+00:00",
        "correct_outcome": "win",
        "actual_winner": "Rublev A.",
    },
]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def correct_journal(journal_path: Path) -> int:
    """Append `settled_correction` rows for each known-wrong settle that
    has not already been corrected. Returns number appended."""
    rows = _read_jsonl(journal_path)
    already_corrected = {r.get("corrects") for r in rows if r.get("type") == "settled_correction"}

    by_pick_id_open = {}
    by_wrong_ts = {}
    for r in rows:
        if r.get("type") == "open":
            by_pick_id_open[r["pick_id"]] = r
        elif r.get("type") == "settled":
            by_wrong_ts[r.get("ts")] = r

    appended = 0
    with open(journal_path, "a", encoding="utf-8") as fh:
        for cfg in WRONG_SETTLES:
            if cfg["wrong_ts"] in already_corrected:
                continue
            wrong = by_wrong_ts.get(cfg["wrong_ts"])
            opened = by_pick_id_open.get(cfg["pick_id"])
            if wrong is None or opened is None:
                continue
            stake = float(wrong.get("stake", opened.get("stake", 25.0)))
            odds = float(wrong.get("sxbet_odds", opened.get("sxbet_odds", 2.0)))
            won = cfg["correct_outcome"] == "win"
            pnl = round(stake * (odds - 1.0), 2) if won else -stake
            entry = {
                "type": "settled_correction",
                "corrects": cfg["wrong_ts"],
                "pick_id": cfg["pick_id"],
                "pick": wrong["pick"],
                "opponent": wrong["opponent"],
                "outcome": cfg["correct_outcome"],
                "pnl": pnl,
                "sxbet_odds": odds,
                "stake": stake,
                "result_winner": cfg["actual_winner"],
                "tournament": wrong.get("tournament"),
                "mode": wrong.get("mode", "dry_run"),
                "ts": datetime.now(timezone.utc).isoformat(),
                "note": "Corrects tiebreak-superscript scraper bug",
            }
            fh.write(json.dumps(entry) + "\n")
            appended += 1
    return appended


def _correction_deltas(rows: list[dict], already_applied: set[str] | None = None) -> dict:
    """For each correction, compute the delta vs the original wrong settle:
    - pnl delta = corrected pnl - original wrong pnl (applied to balance)
    - wins/losses delta = swap counts implied by outcome flip

    Corrections whose `corrects` ts is in `already_applied` are skipped, so
    the same correction is never double-applied across reruns. The
    `applied_ts` list in the return value reports the `corrects` ts values
    actually consumed in this call.

    Returns aggregate balance_delta, wins_delta, losses_delta, applied_ts.
    """
    already_applied = already_applied or set()
    settled_by_ts = {r.get("ts"): r for r in rows if r.get("type") == "settled"}
    balance_delta = 0.0
    wins_delta = 0
    losses_delta = 0
    applied_ts: list[str] = []
    for r in rows:
        if r.get("type") != "settled_correction":
            continue
        corrects = r.get("corrects")
        if corrects in already_applied:
            continue
        wrong = settled_by_ts.get(corrects)
        if wrong is None:
            continue
        balance_delta += float(r["pnl"]) - float(wrong["pnl"])
        if wrong["outcome"] == "win" and r["outcome"] == "loss":
            wins_delta -= 1
            losses_delta += 1
        elif wrong["outcome"] == "loss" and r["outcome"] == "win":
            wins_delta += 1
            losses_delta -= 1
        applied_ts.append(corrects)
    return {
        "balance_delta": balance_delta,
        "wins_delta": wins_delta,
        "losses_delta": losses_delta,
        "applied_ts": applied_ts,
    }


def recompute_state(journal_path: Path, state_path: Path,
                    starting_balance: float = 500.0) -> dict:
    """Adjust state.json balance/total_pnl/wins/losses by applying the
    deltas implied by `settled_correction` rows in the journal against the
    original wrong settle rows they reference. Writes the file. Returns
    the new state dict.

    Idempotent: each correction's `corrects` ts is recorded in
    `state["applied_corrections"]` after being applied; subsequent reruns
    skip those entries so the balance does not drift on re-execution.

    `starting_balance` is retained for API stability but unused: the existing
    state's balance is the authoritative pre-correction value (the live
    journal contains many more settled rows than tests reproduce, so
    recomputing from scratch is unsafe).
    """
    rows = _read_jsonl(journal_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    already_applied = set(state.get("applied_corrections", []) or [])
    deltas = _correction_deltas(rows, already_applied=already_applied)
    state["balance"] = round(float(state.get("balance", 0.0)) + deltas["balance_delta"], 4)
    state["total_pnl"] = round(float(state.get("total_pnl", 0.0)) + deltas["balance_delta"], 4)
    state["wins"] = int(state.get("wins", 0)) + deltas["wins_delta"]
    state["losses"] = int(state.get("losses", 0)) + deltas["losses_delta"]
    state["applied_corrections"] = sorted(already_applied | set(deltas["applied_ts"]))
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    journal = here / ".tmp" / "trades.jsonl"
    state = here / ".tmp" / "state.json"
    n = correct_journal(journal)
    print(f"Appended {n} correction(s).")
    new_state = recompute_state(journal, state)
    print(f"New balance: {new_state['balance']}")
    print(f"New total_pnl: {new_state['total_pnl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
