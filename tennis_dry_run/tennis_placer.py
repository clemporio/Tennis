"""Tennis placer — late-binding T-15 min order placement.

Invoked once per qualifying selection by `at` (or directly by the identifier
for matches starting within the lead-time window). Loads the previously-
identified selection, fetches the SX Bet orderbook close to match start
(when liquidity is real, not the thin junk seen hours earlier), re-applies
the odds-range and edge filters, and submits via TennisExecutor.

CLI: python tennis_placer.py PICK_ID
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis_dry_run import (  # noqa: E402
    JOURNAL_FILE,
    MAX_ODDS,
    MIN_ODDS,
    PAPER_STAKE,
    SKIPPED_FILE,
    STATE_DIR,
    STATE_FILE,
    append_journal,
    load_state,
    save_state,
)

log = logging.getLogger("tennis_placer")


def load_selection(pending_file: Path, pick_id: str) -> Optional[dict]:
    """Find the most recent entry in pending_selections.jsonl matching pick_id."""
    if not pending_file.exists():
        return None
    latest: Optional[dict] = None
    with open(pending_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("pick_id") == pick_id:
                latest = rec
    return latest


def place_pick(
    pick_id: str,
    pending_file: Path,
    state_file: Path,
    sxbet,
    executor,
    trades_file: Optional[Path] = None,
) -> dict:
    """Run the placement pipeline for one selection.

    Returns a dict with keys `status` and (if skipped/failed) `reason`.
    `status` ∈ {"placed", "skipped", "failed", "already_placed", "missing"}.
    """
    selection = load_selection(pending_file, pick_id)
    if selection is None:
        log.error("place_pick: no pending selection for pick_id=%s", pick_id)
        return {"status": "missing", "reason": "no_pending_selection"}

    state = json.loads(state_file.read_text(encoding="utf-8"))
    if pick_id in state.get("open_picks", {}):
        log.info("place_pick: %s already in open_picks, skipping", pick_id)
        return {"status": "already_placed"}

    market_hash = selection["market_hash"]
    pick_name = selection["pick"]
    market_player_a = selection["market_player_a"]

    try:
        odds_info = sxbet.get_best_back_odds(market_hash, pick_name, market_player_a)
    except Exception as exc:
        log.warning("place_pick: get_best_back_odds failed for %s: %s", pick_id, exc)
        return {"status": "failed", "reason": f"sxbet_error:{exc}"}

    if odds_info is None:
        return _skip(selection, "no_liquidity", state_file=state_file)

    sxbet_odds = float(odds_info["decimal_odds"])
    if not (MIN_ODDS <= sxbet_odds <= MAX_ODDS):
        return _skip(selection, "odds_out_of_range_at_placement",
                     state_file=state_file, sxbet_odds=sxbet_odds)

    available_usd = float(odds_info["available_usd"])
    if available_usd < PAPER_STAKE:
        return _skip(selection, "insufficient_liquidity",
                     state_file=state_file,
                     sxbet_odds=sxbet_odds, available_usd=available_usd)

    implied_prob = 1.0 / sxbet_odds
    edge = round(selection["model_prob"] - implied_prob, 4)
    if edge < 0:
        return _skip(selection, "negative_edge",
                     state_file=state_file,
                     sxbet_odds=sxbet_odds, edge=edge)

    pick_context = {
        "pick_id": pick_id,
        "pick": pick_name,
        "opponent": selection["opponent"],
        "league": selection["league"],
        "surface": selection["surface"],
        "round": selection["round"],
        "model_prob": selection["model_prob"],
        "fair_odds": selection["fair_odds"],
        "sxbet_odds": sxbet_odds,
        "sxbet_available_usd": available_usd,
        "implied_prob": round(implied_prob, 4),
        "edge": edge,
        "market_hash": market_hash,
        "is_pick_outcome_one": selection["is_pick_outcome_one"],
        "game_time": selection.get("game_time"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    result = executor.place_order(pick_context)

    if result.status == "blocked":
        return _skip(selection, f"executor_block:{result.block_reason}",
                     state_file=state_file)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.setdefault("open_picks", {})[pick_id] = result.trade_entry
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    journal_path = trades_file if trades_file is not None else JOURNAL_FILE
    append_journal(result.trade_entry, journal_path)

    log.info("place_pick: PLACED %s @ %.3f via executor (status=%s)",
             pick_id, sxbet_odds, result.status)
    return {
        "status": "placed",
        "executor_status": result.status,
        "trade_entry": result.trade_entry,
    }


def _skip(selection: dict, reason: str, *, state_file: Path = None, **extra) -> dict:
    """Log a skip to the standard skipped.jsonl journal and return result dict."""
    entry = {
        "type": "skipped",
        "source": "placer",
        "reason": reason,
        "pick_id": selection["pick_id"],
        "pick": selection.get("pick"),
        "opponent": selection.get("opponent"),
        "league": selection.get("league"),
        "ts": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    append_journal(entry, SKIPPED_FILE)
    log.info("place_pick: SKIP %s reason=%s extras=%s",
             selection["pick_id"], reason, extra)
    return {"status": "skipped", "reason": reason, **extra}


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    argv = argv or sys.argv[1:]
    if len(argv) < 1:
        log.error("Usage: tennis_placer.py PICK_ID")
        return 2
    pick_id = argv[0]

    pending_file = Path(os.getenv(
        "PENDING_SELECTIONS_FILE",
        str(STATE_DIR / "pending_selections.jsonl"),
    ))
    state_file = Path(os.getenv("STATE_FILE", str(STATE_FILE)))

    from tennis_sxbet import TennisSXBet
    from tennis_executor import ExecutorConfig, TennisExecutor

    sxbet = TennisSXBet()
    config = ExecutorConfig.from_env(STATE_DIR)
    executor = TennisExecutor(config)

    state = load_state()
    executor.set_today_live_stake(state.get("today_live_stake", 0.0))

    result = place_pick(pick_id, pending_file, state_file, sxbet, executor)
    log.info("placer result: %s", result)
    return 0 if result["status"] in ("placed", "already_placed", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
