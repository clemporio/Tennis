"""Tennis shadow placer — observation-only T-90min order evaluator.

Mirrors `tennis_placer.place_pick`'s decision logic, but never calls the
executor and never mutates state. Output goes to `.tmp/shadow_placements.jsonl`.

Purpose: A/B comparison between tier-A (placed at T-15) and tier-B (shadow at
T-90). Both record the same shape of decision data — sxbet_odds at fire time,
edge, would-place vs would-skip with reason — letting the EOD report quantify
which lead-time captures more signal.

Invoked once per tier-B selection by an `at` job scheduled by the identifier.

CLI: python tennis_shadow_placer.py PICK_ID
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
    MAX_ODDS,
    MIN_ODDS,
    PAPER_STAKE,
    STATE_DIR,
)
from tennis_sxbet import TennisSXBet  # noqa: E402  (top-level for monkeypatch)

log = logging.getLogger("tennis_shadow_placer")


def evaluate_shadow_placement(
    selection: dict,
    sxbet,
    paper_stake: float = PAPER_STAKE,
) -> dict:
    """Decide what `place_pick` *would* have done for this selection right now.

    Pure decision: fetches the orderbook (via `sxbet.get_best_back_odds`),
    applies the same odds-range / liquidity / edge gates as the real placer,
    and returns a structured result. Does NOT touch executor, state, or any
    file. Caller is responsible for journalling via `append_shadow_placement`.

    Args:
        selection: pick dict (same shape as `pending_selections.jsonl` rows).
        sxbet: TennisSXBet (or duck-typed equivalent) for orderbook query.
        paper_stake: base taker stake the bot would attempt — used only to
            classify "insufficient_liquidity" vs "would_place".

    Returns:
        Dict with `status` ∈ {"would_place", "would_skip"} and (when known)
        `sxbet_odds`, `available_usd`, `implied_prob`, `edge`, `reason`,
        plus pick metadata + `ts` + `source="shadow_placer"`.
    """
    base = {
        "source": "shadow_placer",
        "pick_id": selection["pick_id"],
        "pick": selection.get("pick"),
        "opponent": selection.get("opponent"),
        "league": selection.get("league"),
        "model_prob": selection.get("model_prob"),
        "fair_odds": selection.get("fair_odds"),
        "tier": selection.get("tier"),
        "market_hash": selection.get("market_hash"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    try:
        odds_info = sxbet.get_best_back_odds(
            selection["market_hash"],
            selection["pick"],
            selection["market_player_a"],
        )
    except Exception as exc:
        log.warning("evaluate_shadow_placement: sxbet error for %s: %s",
                    selection["pick_id"], exc)
        return {**base, "status": "would_skip",
                "reason": f"sxbet_error:{exc}"}

    if odds_info is None:
        return {**base, "status": "would_skip", "reason": "no_liquidity"}

    sxbet_odds = float(odds_info["decimal_odds"])
    available_usd = float(odds_info["available_usd"])
    implied_prob = 1.0 / sxbet_odds
    edge = round(float(selection["model_prob"]) - implied_prob, 4)
    base.update({
        "sxbet_odds": sxbet_odds,
        "available_usd": available_usd,
        "implied_prob": round(implied_prob, 4),
        "edge": edge,
    })

    if not (MIN_ODDS <= sxbet_odds <= MAX_ODDS):
        return {**base, "status": "would_skip",
                "reason": "odds_out_of_range_at_placement"}

    if available_usd < paper_stake:
        return {**base, "status": "would_skip",
                "reason": "insufficient_liquidity"}

    if edge < 0:
        return {**base, "status": "would_skip", "reason": "negative_edge"}

    return {**base, "status": "would_place"}


def append_shadow_placement(record: dict, placements_file: Path) -> None:
    """Append one shadow placement record as a single JSON line."""
    placements_file.parent.mkdir(parents=True, exist_ok=True)
    with open(placements_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_shadow_selection(shadow_file: Path, pick_id: str) -> Optional[dict]:
    """Find the most recent entry in shadow_selections.jsonl matching pick_id."""
    if not shadow_file.exists():
        return None
    latest: Optional[dict] = None
    with open(shadow_file, "r", encoding="utf-8") as f:
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


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    argv = argv or sys.argv[1:]
    if len(argv) < 1:
        log.error("Usage: tennis_shadow_placer.py PICK_ID")
        return 2
    pick_id = argv[0]

    shadow_sel_file = Path(os.getenv(
        "SHADOW_SELECTIONS_FILE",
        str(STATE_DIR / "shadow_selections.jsonl"),
    ))
    shadow_pl_file = Path(os.getenv(
        "SHADOW_PLACEMENTS_FILE",
        str(STATE_DIR / "shadow_placements.jsonl"),
    ))

    selection = load_shadow_selection(shadow_sel_file, pick_id)
    if selection is None:
        log.error("No shadow selection found for pick_id=%s in %s",
                  pick_id, shadow_sel_file)
        return 1

    sxbet = TennisSXBet()

    result = evaluate_shadow_placement(selection, sxbet)
    append_shadow_placement(result, shadow_pl_file)
    log.info("Shadow eval %s: %s (sxbet_odds=%s edge=%s)",
             pick_id, result["status"],
             result.get("sxbet_odds"), result.get("edge"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
