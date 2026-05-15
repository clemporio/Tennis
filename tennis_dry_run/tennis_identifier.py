"""Tennis identifier — morning one-shot that schedules per-pick placers.

Runs once daily (cron 07:00 UTC by default). Fetches today's SX Bet match-
winner markets, runs the model + Elo + filter pipeline, and for each
qualifying selection either:
  - schedules an `at` job at (gameTime - 15 min) to invoke tennis_placer.py, OR
  - invokes the placer synchronously when the match starts within 15 min.

Late-binding the orderbook fetch to T-15 sidesteps thin-book outliers seen
hours before kickoff. See plan: where-the-work-lives-delegated-wilkes.md.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Reuse helpers and constants from the scan-loop module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis_dry_run import (  # noqa: E402
    ELO_FILE,
    MAX_DAILY_BETS,
    MAX_ODDS,
    MIN_CONFIDENCE,
    MIN_ODDS,
    ROUNDS_FILTER,
    STATE_DIR,
    _apply_settled_corrections,
    _build_model_input,
    _detect_surface,
    _extract_last_and_initial,
    _find_player_elo,
    load_state,
    scrape_scheduled_matches,
)

# Shadow-tier threshold: picks in [SHADOW_MIN_CONFIDENCE, MIN_CONFIDENCE) are
# tagged tier="B" and recorded but NOT scheduled for placement. Used to gather
# a paper trail of what a lower-confidence model would have selected, against
# which we can compare hit rate / theoretical PnL without placement risk.
SHADOW_MIN_CONFIDENCE = float(os.getenv("SHADOW_MIN_CONFIDENCE", "0.70"))

# Hard reject any market whose league name matches these (case-insensitive)
# substrings. Prior backtest showed the model can't predict challenger /
# qualifying / ITF reliably (sparse Elo, lower-tier players, different
# volatility), and the training data is tour-only so these would be OOD.
EXCLUDED_LEAGUE_SUBSTRINGS = ("challenger", "qualifying", "qualif.", " q1", " q2",
                              " q3", "itf ")


def _is_excluded_league(league: str) -> bool:
    """True if `league` matches any excluded-tier substring."""
    if not league:
        return False
    low = league.lower()
    return any(sub in low for sub in EXCLUDED_LEAGUE_SUBSTRINGS)


def _daily_cap_remaining(state: dict, qualified_this_run: int,
                         now_utc: datetime, cap: int) -> int:
    """How many more tier-A picks can be qualified before the daily cap.

    `state.today_bets` is the bot's persisted counter; treat as zero when
    `state.today_date` is missing or stale (yesterday or earlier), since the
    counter resets at UTC midnight via placer rollover.
    """
    today_str = now_utc.strftime("%Y-%m-%d")
    already_today = int(state.get("today_bets", 0)) if state.get("today_date") == today_str else 0
    return max(0, cap - already_today - qualified_this_run)


def _is_on_date(ts_iso: str, target) -> bool:
    if not ts_iso:
        return False
    try:
        return datetime.fromisoformat(ts_iso).astimezone(timezone.utc).date() == target
    except Exception:
        return False

log = logging.getLogger("tennis_identifier")


def today_window(now_utc: datetime) -> tuple[datetime, datetime]:
    """Returns (now_utc, end_of_today_utc) for filtering markets.

    Args:
        now_utc: Current UTC datetime.

    Returns:
        Tuple of (start, end) where end is 23:59:59.999999 of the same UTC date.
    """
    end = now_utc.replace(hour=23, minute=59, second=59, microsecond=999999)
    return (now_utc, end)


def filter_today_markets(markets: list[dict], now_utc: datetime) -> list[dict]:
    """Keep only markets whose gameTime is in [now_utc, end_of_today_utc].

    Args:
        markets: Normalized market dicts with `game_time` (Unix timestamp seconds).
        now_utc: Current UTC datetime.

    Returns:
        Subset of markets starting today and not already started.
    """
    start, end = today_window(now_utc)
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    return [m for m in markets if start_ts <= m.get("game_time", 0) <= end_ts]


def evaluate_market(
    market: dict,
    elo_data: dict,
    predictor,
    te_round_map: dict,
    state: dict,
    now_utc: datetime,
) -> tuple[Optional[dict], Optional[str]]:
    """Run the discovery pipeline on a single market.

    Returns:
        (selection, skip_reason) tuple:
          - On success: (selection_dict, None) — selection has full placer payload.
          - On skip: (None, reason_key) — reason_key is one of:
            "dedup", "excluded_league", "round", "no_elo", "no_pred",
            "low_conf", "odds".

    Short-circuits on dedup against state.open_picks before any model work.
    """
    market_hash = market["market_hash"]
    if market_hash in state.get("open_picks", {}):
        return None, "dedup"

    player_a = market["player_a"]
    player_b = market["player_b"]
    league = market.get("league", "")

    if _is_excluded_league(league):
        return None, "excluded_league"

    surface = _detect_surface(league)

    la, _ = _extract_last_and_initial(player_a)
    lb, _ = _extract_last_and_initial(player_b)
    round_key = tuple(sorted([la, lb]))
    match_round = te_round_map.get(round_key, "unknown")
    if match_round != "unknown" and match_round not in ROUNDS_FILTER:
        return None, "round"

    pa_elo = _find_player_elo(player_a, elo_data)
    pb_elo = _find_player_elo(player_b, elo_data)
    if not pa_elo or not pb_elo:
        return None, "no_elo"

    pa_input = _build_model_input(pa_elo, surface)
    pb_input = _build_model_input(pb_elo, surface)
    call_args = dict(pa_input)
    for k, v in pb_input.items():
        call_args["pb_" + k[3:]] = v
    call_args["surface"] = surface
    pred = predictor.predict_match(**call_args)
    if not pred:
        return None, "no_pred"

    prob_a = pred["prob_a"]
    prob_b = pred["prob_b"]

    if prob_a >= prob_b:
        pick_name, opponent_name, pick_prob = player_a, player_b, prob_a
    else:
        pick_name, opponent_name, pick_prob = player_b, player_a, prob_b

    if pick_prob < SHADOW_MIN_CONFIDENCE:
        return None, "low_conf"

    fair_odds = round(1.0 / pick_prob, 3)
    if not (MIN_ODDS <= fair_odds <= MAX_ODDS):
        return None, "odds"

    tier = "A" if pick_prob >= MIN_CONFIDENCE else "B"

    return {
        "pick_id": market_hash,
        "pick": pick_name,
        "opponent": opponent_name,
        "league": league,
        "surface": surface,
        "round": match_round,
        "model_prob": round(pick_prob, 4),
        "fair_odds": fair_odds,
        "tier": tier,
        "market_hash": market_hash,
        "market_player_a": player_a,
        "is_pick_outcome_one": pick_name.strip() == player_a.strip(),
        "game_time": market.get("game_time"),
        "ts": now_utc.isoformat(),
    }, None


def schedule_or_place(
    selection: dict,
    now_utc: datetime,
    lead_min: int,
    placer_cmd: list[str],
) -> dict:
    """Schedule the placer via `at` (lead_min before gameTime) or invoke
    immediately if the match starts within lead_min.

    Args:
        selection: Output of `evaluate_market`. Must contain `pick_id` and `game_time`.
        now_utc: Current UTC datetime.
        lead_min: Minutes before gameTime to fire the placer.
        placer_cmd: Argv prefix invoking the placer (e.g. ["python", "tennis_placer.py"]).
                    The pick_id is appended as the final argument.

    Returns:
        Dict with `placement_path` ("scheduled" | "immediate") and `scheduled_at_iso`.
    """
    game_time = datetime.fromtimestamp(selection["game_time"], tz=timezone.utc)
    fire_time = game_time - timedelta(minutes=lead_min)
    pick_id = selection["pick_id"]

    if fire_time <= now_utc:
        cmd = list(placer_cmd) + [pick_id]
        subprocess.run(cmd, check=False)
        return {"placement_path": "immediate", "scheduled_at_iso": None}

    fire_time_str = fire_time.strftime("%Y%m%d%H%M")
    full_cmd = " ".join(list(placer_cmd) + [pick_id])
    at_input = f"{full_cmd} 2>&1 | logger -t tennis-placer\n"
    subprocess.run(["at", "-t", fire_time_str], input=at_input, text=True, check=False)
    return {"placement_path": "scheduled", "scheduled_at_iso": fire_time.isoformat()}


def persist_selection(
    selection: dict,
    schedule_outcome: dict,
    pending_file: Path,
) -> None:
    """Append the selection + scheduling outcome as one JSON line."""
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    record = {**selection, **schedule_outcome}
    with open(pending_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def prune_stale_pending(
    pending_file: Path,
    now_utc: datetime,
    grace_minutes: int = 60,
) -> dict:
    """Drop pending selections whose game_time is past `now - grace_minutes`.

    The placer fires at game_time - lead_min and the match itself runs after
    game_time. Once game_time + grace has passed there is no downstream reader
    for the entry — pruning prevents `pending_selections.jsonl` from growing
    unbounded across days.

    Atomic: writes survivors to <file>.tmp, then `os.replace`s into place.
    Malformed JSON lines are silently dropped. Entries without `game_time`
    are kept (unknown timing → don't drop).

    Returns: {"kept": int, "pruned": int, "pruned_picks": list[str]}.
    """
    if not pending_file.exists():
        return {"kept": 0, "pruned": 0, "pruned_picks": []}
    cutoff_ts = (now_utc - timedelta(minutes=grace_minutes)).timestamp()
    kept_lines: list[str] = []
    pruned_picks: list[str] = []
    for raw in pending_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        gt = row.get("game_time")
        if gt is None:
            kept_lines.append(line)
            continue
        try:
            gt_val = float(gt)
        except (TypeError, ValueError):
            kept_lines.append(line)
            continue
        if gt_val >= cutoff_ts:
            kept_lines.append(line)
        else:
            pruned_picks.append(row.get("pick", "?"))
    tmp = pending_file.with_suffix(pending_file.suffix + ".tmp")
    payload = ("\n".join(kept_lines) + "\n") if kept_lines else ""
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, pending_file)
    return {
        "kept": len(kept_lines),
        "pruned": len(pruned_picks),
        "pruned_picks": pruned_picks,
    }


def prune_shadow_stale(
    shadow_file: Path,
    now_utc: datetime,
) -> dict:
    """Prune shadow_selections.jsonl by UTC DATE, not by post-game grace.

    Unlike pending_selections.jsonl (where the placer fires at T-15 and the
    entry has no downstream reader after game_time + grace), shadow_selections
    is read by the 22:00 UTC EOD report. A grace-based prune deletes today's
    completed shadow picks before EOD can resolve them — the 2026-05-11 bug.

    Keeps every entry whose `game_time` falls on today's UTC date; entries
    without `game_time` are also kept (unknown timing → don't drop).

    Atomic via tempfile + os.replace. Malformed JSON lines dropped silently.
    """
    if not shadow_file.exists():
        return {"kept": 0, "pruned": 0, "pruned_picks": []}
    today = now_utc.date()
    kept_lines: list[str] = []
    pruned_picks: list[str] = []
    for raw in shadow_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        gt = row.get("game_time")
        if gt is None:
            kept_lines.append(line)
            continue
        try:
            gt_dt = datetime.fromtimestamp(float(gt), tz=timezone.utc)
        except (TypeError, ValueError):
            kept_lines.append(line)
            continue
        if gt_dt.date() == today:
            kept_lines.append(line)
        else:
            pruned_picks.append(row.get("pick", "?"))
    tmp = shadow_file.with_suffix(shadow_file.suffix + ".tmp")
    payload = ("\n".join(kept_lines) + "\n") if kept_lines else ""
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, shadow_file)
    return {
        "kept": len(kept_lines),
        "pruned": len(pruned_picks),
        "pruned_picks": pruned_picks,
    }


def write_daily_report(
    now_utc: datetime,
    counts: dict,
    selections: list[dict],
    markets_total: int,
    markets_today: int,
    vault_dir: Path,
    state_dir: Path,
    rolling_path: Optional[Path] = None,
    shadow_selections: Optional[list[dict]] = None,
) -> Path:
    """Write daily BOD report + refresh rolling file.

    Daily file: <vault_dir>/YYYY-MM-DD.md — Portfolio + Identified Picks +
        Shadow Picks (tier B, captured but not placed).
    Rolling file (optional): single file rewritten with current snapshot.
    """
    from tennis_kelly import replay_three_bankrolls
    from tennis_portfolio import (
        render_portfolio_block,
        render_open_picks_block,
        render_closed_trades_block,
        render_performance_block,
        render_backtest_comparison_block,
        render_identified_picks_block,
        render_shadow_picks_block,
        render_yesterday_recap_block,
    )

    vault_dir = Path(vault_dir)
    vault_dir.mkdir(parents=True, exist_ok=True)
    date_str = now_utc.date().isoformat()
    out_path = vault_dir / f"{date_str}.md"

    # Load state + trades
    state_file = Path(state_dir) / "state.json"
    trades_file = Path(state_dir) / "trades.jsonl"
    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    all_rows: list[dict] = []
    if trades_file.exists():
        for line in trades_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_rows.append(json.loads(line))
            except Exception:
                continue
    # Apply settled_correction overrides so renderers + replay see the
    # corrected outcome/pnl/result_winner transparently.
    all_rows = _apply_settled_corrections(all_rows)
    placed: list[dict] = [r for r in all_rows if r.get("type") == "open"]
    settled: list[dict] = [r for r in all_rows if r.get("type") == "settled"]

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=now_utc.date())

    yesterday = now_utc.date() - timedelta(days=1)
    replay_yesterday = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                              today=yesterday)
    settled_yesterday = [
        s for s in settled
        if _is_on_date(s.get("ts", ""), yesterday)
    ]
    placed_lookup = {p["pick_id"]: p for p in placed if "pick_id" in p}
    open_picks = state.get("open_picks", {}) or {}

    # `Qualified` reflects what the identifier itself surfaced this run, but
    # the bot's scan-loop can open picks between the identifier loading state
    # and writing the report (race observed 2026-05-13: Gauff opened by bot
    # ~24s after identifier started, was invisible in the headline count).
    # Surface the gap explicitly. Clamped to zero — bot may also have settled
    # picks since the identifier's count was taken, so the diff can go negative.
    bot_opened = max(0, len(open_picks) - counts.get("qualified", 0))

    # Daily file
    lines: list[str] = [
        "---",
        f"date: {date_str}",
        "type: tennis-daily-report",
        "tags: [tennis, dry-run]",
        "---",
        "",
        f"# Tennis Daily Report — {date_str}",
        "",
        f"BOD run timestamp: {now_utc.replace(microsecond=0).isoformat()}",
        "",
        render_yesterday_recap_block(yesterday, settled_yesterday, placed_lookup, replay=replay_yesterday),
        render_portfolio_block(replay, now_utc),
        render_open_picks_block(open_picks, replay),
        "## Scan Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Markets total | {markets_total} |",
        f"| Markets today | {markets_today} |",
        f"| Qualified | {counts.get('qualified', 0)} |",
        f"| Bot-opened (not counted in Qualified) | {bot_opened} |",
        f"| Scheduled (`at`) | {counts.get('scheduled', 0)} |",
        f"| Placed immediately | {counts.get('immediate', 0)} |",
        f"| Shadow (tier B, 70-80%) | {counts.get('shadow', 0)} |",
        f"| Skipped (dedup) | {counts.get('skipped_dedup', 0)} |",
        f"| Skipped (filter) | {counts.get('skipped_filter', 0)} |",
        f"| · no_elo | {counts.get('skipped_no_elo', 0)} |",
        f"| · low_conf | {counts.get('skipped_low_conf', 0)} |",
        f"| · round | {counts.get('skipped_round', 0)} |",
        f"| · fair_odds_out_of_range | {counts.get('skipped_odds', 0)} |",
        f"| · excluded_league | {counts.get('skipped_excluded_league', 0)} |",
        f"| · no_pred | {counts.get('skipped_no_pred', 0)} |",
        "",
        render_identified_picks_block(selections),
        render_shadow_picks_block(shadow_selections or []),
        "---",
        f"_Generated by `tennis_identifier.py` on the LXII Capital VPS._",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Rolling file (full re-render)
    if rolling_path is not None:
        rolling_lines: list[str] = [
            "---",
            "tags: [tennis, dry-run, report]",
            "type: report",
            "---",
            "",
            f"## Tennis Dry Run Report — {now_utc.strftime('%Y-%m-%d %H:%M')} UTC",
            "",
            render_portfolio_block(replay, now_utc),
            render_open_picks_block(open_picks, replay),
            render_closed_trades_block(settled, placed, n=30),
            render_performance_block(replay),
            render_backtest_comparison_block(replay),
            "---",
            f"_Generated by `tennis_identifier.py` at "
            f"{now_utc.strftime('%H:%M')} UTC._",
            "",
        ]
        Path(rolling_path).parent.mkdir(parents=True, exist_ok=True)
        Path(rolling_path).write_text("\n".join(rolling_lines), encoding="utf-8")

    return out_path


def _build_te_round_map() -> dict:
    """Best-effort: scrape TennisExplorer to get a round map for today's matches.
    Failures here just leave the map empty — the identifier passes through
    "unknown" rounds (soft filter only blocks positively-identified non-target)."""
    try:
        te_matches = scrape_scheduled_matches()
    except Exception as exc:
        log.warning("TennisExplorer round scrape failed: %s", exc)
        return {}

    round_map: dict = {}
    for m in te_matches:
        la, _ = _extract_last_and_initial(m["player_a"])
        lb, _ = _extract_last_and_initial(m["player_b"])
        if not la or not lb:
            continue
        round_map[tuple(sorted([la, lb]))] = m.get("round", "unknown")
    return round_map


def main() -> int:
    """Cron entry point. Scans today's SX Bet markets, schedules placers."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    from tennis_sxbet import TennisSXBet
    from tennis_model.predictor import TennisModelPredictor

    lead_min = int(os.getenv("PLACEMENT_LEAD_MIN", "15"))
    pending_file = Path(os.getenv(
        "PENDING_SELECTIONS_FILE",
        str(STATE_DIR / "pending_selections.jsonl"),
    ))
    shadow_file = Path(os.getenv(
        "SHADOW_SELECTIONS_FILE",
        str(STATE_DIR / "shadow_selections.jsonl"),
    ))
    vault_dir_env = os.getenv(
        "OBSIDIAN_VAULT_DIR",
        "/opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports",
    )
    vault_dir = Path(vault_dir_env) if vault_dir_env else None

    placer_path = Path(__file__).resolve().parent / "tennis_placer.py"
    venv_python = os.getenv("PLACER_PYTHON", sys.executable)
    placer_cmd = [venv_python, str(placer_path)]
    shadow_placer_path = Path(__file__).resolve().parent / "tennis_shadow_placer.py"
    shadow_placer_cmd = [venv_python, str(shadow_placer_path)]
    shadow_lead_min = int(os.getenv("SHADOW_PLACEMENT_LEAD_MIN", "90"))

    now_utc = datetime.now(timezone.utc)
    log.info("Identifier run starting at %s UTC (lead_min=%d)",
             now_utc.isoformat(), lead_min)

    prune_grace_min = int(os.getenv("PENDING_PRUNE_GRACE_MIN", "60"))
    # shadow_placements.jsonl is an append-only audit log (keyed by ts, not
    # game_time) — don't prune it. It grows ~one line per tier-B pick per day.
    for label, fpath in (("pending", pending_file), ("shadow", shadow_file)):
        try:
            r = prune_stale_pending(fpath, now_utc, grace_minutes=prune_grace_min)
            if r["pruned"]:
                log.info(
                    "Pruned %d stale %s selection(s) (kept=%d): %s",
                    r["pruned"], label, r["kept"],
                    ", ".join(r["pruned_picks"]),
                )
        except Exception as exc:
            log.warning("%s prune failed: %s", label, exc)

    sxbet = TennisSXBet()
    try:
        all_markets = sxbet.get_all_tennis_markets()
    except Exception as exc:
        log.error("Failed to fetch SX Bet markets: %s", exc)
        return 1

    today_markets = filter_today_markets(all_markets, now_utc)
    log.info("Markets: %d total, %d today", len(all_markets), len(today_markets))

    if not today_markets:
        log.info("No markets today — exiting.")
        return 0

    elo_data: dict = {}
    if ELO_FILE.exists():
        try:
            elo_data = json.loads(ELO_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load Elo file: %s", exc)
    log.info("Elo entries loaded: %d", len(elo_data))

    predictor = TennisModelPredictor()
    if not predictor.load():
        log.error("Failed to load tennis model")
        return 1
    predictor.MIN_CONFIDENCE = 0.0

    te_round_map = _build_te_round_map()
    log.info("Round map entries: %d", len(te_round_map))

    state = load_state()

    counts = {
        "qualified": 0, "scheduled": 0, "immediate": 0,
        "skipped_dedup": 0, "skipped_filter": 0,
        "skipped_no_elo": 0, "skipped_low_conf": 0,
        "skipped_round": 0, "skipped_odds": 0,
        "skipped_excluded_league": 0, "skipped_no_pred": 0,
        "skipped_daily_cap": 0,
        "shadow": 0,
    }
    daily_cap = int(os.getenv("TENNIS_MAX_DAILY_BETS", str(MAX_DAILY_BETS)))
    selections_for_report: list[dict] = []
    shadow_for_report: list[dict] = []

    for market in today_markets:
        if market["market_hash"] in state.get("open_picks", {}):
            counts["skipped_dedup"] += 1
            continue
        selection, skip_reason = evaluate_market(
            market, elo_data, predictor, te_round_map, state, now_utc
        )
        if selection is None:
            counts[f"skipped_{skip_reason}"] = counts.get(f"skipped_{skip_reason}", 0) + 1
            counts["skipped_filter"] += 1
            continue

        game_time_iso = datetime.fromtimestamp(
            selection["game_time"], tz=timezone.utc
        ).isoformat()

        if selection.get("tier") == "B":
            # Shadow track: never placed. Persist the selection, then schedule
            # an `at` job at T-shadow_lead_min that fires `tennis_shadow_placer`
            # — fetches the orderbook, runs the same gates as the real placer,
            # writes a "would_place" / "would_skip" record. Pure observation,
            # no executor, no state mutation. Drives A/B comparison vs tier-A's
            # T-15 timing.
            shadow_outcome = schedule_or_place(
                selection, now_utc, shadow_lead_min, shadow_placer_cmd,
            )
            shadow_outcome["placement_path"] = "shadow"  # override label
            persist_selection(selection, shadow_outcome, shadow_file)
            counts["shadow"] += 1
            shadow_for_report.append({
                **selection,
                "game_time_iso": game_time_iso,
                "placement_path": "shadow",
                "scheduled_at_iso": shadow_outcome.get("scheduled_at_iso"),
            })
            log.info("Shadow selection (tier B): %s vs %s @ %s (prob=%.4f, "
                     "shadow_fire=%s)",
                     selection["pick"], selection["opponent"],
                     game_time_iso, selection["model_prob"],
                     shadow_outcome.get("scheduled_at_iso") or "now")
            continue

        # Tier A — placement track.
        if _daily_cap_remaining(state, counts["qualified"], now_utc, daily_cap) <= 0:
            counts["skipped_daily_cap"] += 1
            counts["skipped_filter"] += 1
            log.info("identifier: daily cap %d reached (already_today=%d, "
                     "qualified_this_run=%d) — skipping further tier-A picks",
                     daily_cap, int(state.get("today_bets", 0)),
                     counts["qualified"])
            continue
        counts["qualified"] += 1
        outcome = schedule_or_place(selection, now_utc, lead_min, placer_cmd)
        if outcome["placement_path"] == "scheduled":
            counts["scheduled"] += 1
        else:
            counts["immediate"] += 1
        persist_selection(selection, outcome, pending_file)
        selections_for_report.append({
            **selection,
            "game_time_iso": game_time_iso,
            "placement_path": outcome["placement_path"],
            "scheduled_at_iso": outcome.get("scheduled_at_iso"),
        })
        log.info("Selection: %s @ %s (path=%s)",
                 selection["pick"], outcome.get("scheduled_at_iso") or "now",
                 outcome["placement_path"])

    log.info(
        "Summary: qualified=%d scheduled=%d immediate=%d "
        "shadow=%d skipped_dedup=%d skipped_filter=%d",
        counts["qualified"], counts["scheduled"], counts["immediate"],
        counts["shadow"], counts["skipped_dedup"], counts["skipped_filter"],
    )

    if vault_dir is not None:
        try:
            rolling_path_env = os.getenv(
                "TENNIS_ROLLING_REPORT",
                "/opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Tennis-Dry-Run-Report.md",
            )
            rolling_path = Path(rolling_path_env) if rolling_path_env else None
            report_path = write_daily_report(
                now_utc, counts, selections_for_report,
                len(all_markets), len(today_markets),
                vault_dir=vault_dir,
                state_dir=STATE_DIR,
                rolling_path=rolling_path,
                shadow_selections=shadow_for_report,
            )
            log.info("Daily report written: %s", report_path)
            if rolling_path:
                log.info("Rolling report refreshed: %s", rolling_path)
        except Exception as exc:
            log.warning("Vault report write failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
