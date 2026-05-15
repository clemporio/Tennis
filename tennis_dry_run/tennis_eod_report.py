"""Tennis EOD report — daily performance summary appended to Obsidian vault.

Run by cron at 22:00 UTC. Reads state.json, trades.jsonl, skipped.jsonl
and pending_selections.jsonl, aggregates today's activity, and appends
(or replaces) an "EOD Performance" section to <vault>/YYYY-MM-DD.md.

CLI: python tennis_eod_report.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis_dry_run import (  # noqa: E402
    STATE_DIR,
    _apply_settled_corrections,
    match_player_name,
)

log = logging.getLogger("tennis_eod_report")


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _is_today(ts_iso: str, today: date) -> bool:
    if not ts_iso:
        return False
    try:
        return (
            datetime.fromisoformat(ts_iso).astimezone(timezone.utc).date() == today
        )
    except Exception:
        return False


def resolve_shadow_outcomes(
    shadow_selections: list[dict],
    completed_results: list[dict],
    base_stake_usd: float = 25.0,
) -> list[dict]:
    """Pair each shadow selection with a completed match result, if any.

    Returns a list of selection dicts enriched with:
      - status: "WIN" | "LOSS" | "pending"
      - theoretical_pnl: float (0 if pending). For WIN, stake * (fair_odds - 1);
        for LOSS, -stake. Uses fair_odds because shadow picks never bind to a
        real T-15 SX Bet price — this is the model-implied PnL, not realised.
      - result_winner: TennisExplorer winner string, or None if pending.
    Original selection keys are preserved.
    """
    enriched: list[dict] = []
    for sel in shadow_selections:
        pick_player = sel.get("pick", "")
        opponent = sel.get("opponent", "")
        fair_odds = float(sel.get("fair_odds", 1.0))

        match = None
        for r in completed_results:
            ra = r.get("player_a", "")
            rb = r.get("player_b", "")
            order_1 = match_player_name(pick_player, ra) and match_player_name(opponent, rb)
            order_2 = match_player_name(pick_player, rb) and match_player_name(opponent, ra)
            if order_1 or order_2:
                match = r
                break

        if match is None:
            enriched.append({**sel, "status": "pending",
                             "theoretical_pnl": 0.0, "result_winner": None})
            continue

        # Retirement → void the shadow pick (status="RETIRED", pnl=0). A
        # retirement is neither a WIN nor a LOSS; treating it as LOSS would
        # corrupt shadow win-rate / theoretical PnL aggregates. Mirrors the
        # bot's settlement journal which uses outcome="retired".
        if match.get("retired"):
            enriched.append({**sel, "status": "RETIRED",
                             "theoretical_pnl": 0.0,
                             "result_winner": match.get("winner")})
            continue

        won = match_player_name(pick_player, match.get("winner", ""))
        if won:
            pnl = base_stake_usd * (fair_odds - 1.0)
            status = "WIN"
        else:
            pnl = -base_stake_usd
            status = "LOSS"
        enriched.append({**sel, "status": status,
                         "theoretical_pnl": round(pnl, 2),
                         "result_winner": match.get("winner")})
    return enriched


def append_shadow_outcomes_log(
    enriched: list[dict],
    log_file: Path,
    now_utc: datetime,
) -> int:
    """Append resolved shadow rows to `shadow_outcomes.jsonl` with idempotent
    `(pick_id, status)` dedup. Pending rows are NOT written.

    Returns the number of new rows appended. The log is the single source of
    truth for tier-B calibration analysis; per-day markdown reports are derived.
    """
    resolved = [r for r in enriched if r.get("status") in ("WIN", "LOSS", "RETIRED")]
    if not resolved:
        return 0

    existing_keys: set = set()
    if log_file.exists():
        for raw in log_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            pid = row.get("pick_id")
            st = row.get("status")
            if pid and st:
                existing_keys.add((pid, st))

    resolved_at_iso = now_utc.replace(microsecond=0).isoformat()
    appended = 0
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        for row in resolved:
            key = (row.get("pick_id"), row.get("status"))
            if key in existing_keys:
                continue
            out_row = {**row, "resolved_at": resolved_at_iso}
            f.write(json.dumps(out_row, default=str) + "\n")
            existing_keys.add(key)
            appended += 1
    return appended


def write_eod_report(
    now_utc: datetime,
    state_dir: Path,
    vault_dir: Path,
    rolling_path: Optional[Path] = None,
) -> Path:
    """Append/replace EOD section in today's daily file + re-render rolling file."""
    from tennis_kelly import replay_three_bankrolls
    from tennis_portfolio import (
        render_portfolio_block,
        render_open_picks_block,
        render_closed_trades_block,
        render_performance_block,
        render_backtest_comparison_block,
        render_today_placer_activity_block,
        render_today_settlements_block,
        render_stale_carryover_block,
        render_placer_rejection_diagnostics_block,
        render_shadow_picks_block,
    )

    today = now_utc.date()
    today_iso = today.isoformat()

    state_file = Path(state_dir) / "state.json"
    trades_file = Path(state_dir) / "trades.jsonl"
    skipped_file = Path(state_dir) / "skipped.jsonl"
    pending_file = Path(state_dir) / "pending_selections.jsonl"
    shadow_file = Path(state_dir) / "shadow_selections.jsonl"
    shadow_placements_file = Path(state_dir) / "shadow_placements.jsonl"

    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    all_trades = _read_jsonl(trades_file)
    # Apply settled_correction overrides so renderers + replay see the
    # corrected outcome/pnl/result_winner transparently.
    all_trades = _apply_settled_corrections(all_trades)
    all_skipped = _read_jsonl(skipped_file)
    skipped_today = [s for s in all_skipped if _is_today(s.get("ts", ""), today)]
    all_placer_skips = [s for s in all_skipped if s.get("source") == "placer"]

    placed = [t for t in all_trades if t.get("type") == "open"]
    settled = [t for t in all_trades if t.get("type") == "settled"]
    placed_today = [p for p in placed if _is_today(p.get("ts", ""), today)]
    settled_today = [s for s in settled if _is_today(s.get("ts", ""), today)]
    placed_lookup = {p["pick_id"]: p for p in placed}
    placer_skips_today = [s for s in skipped_today if s.get("source") == "placer"]
    pending = _read_jsonl(pending_file)

    # Filter shadow selections to today's matches (by game_time UTC date).
    shadow_today: list[dict] = []
    for row in _read_jsonl(shadow_file):
        gt = row.get("game_time")
        if gt is None:
            continue
        try:
            gt_dt = datetime.fromtimestamp(float(gt), tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        if gt_dt.date() != today:
            continue
        if "game_time_iso" not in row:
            row = {**row, "game_time_iso": gt_dt.isoformat()}
        shadow_today.append(row)

    # Merge T-90 shadow_placements (latest per pick_id) so the renderer can
    # show would-place vs skip-reason at the comparison fire time.
    placements_latest: dict = {}
    for p in _read_jsonl(shadow_placements_file):
        pid = p.get("pick_id")
        if not pid:
            continue
        prev = placements_latest.get(pid)
        if prev is None or p.get("ts", "") > prev.get("ts", ""):
            placements_latest[pid] = p
    for sel in shadow_today:
        sp = placements_latest.get(sel.get("pick_id"))
        if sp:
            sel["shadow_placement"] = sp

    # Enrich shadow picks with outcomes from TennisExplorer (best-effort —
    # network failure leaves picks as `pending`, never crashes the report).
    # Pass `today` so the scraper pins TE's view to the right calendar day
    # (default URL returns Prague-time "today", which rolls over to tomorrow
    # before the 22:00 UTC cron and would return zero matches).
    if shadow_today:
        try:
            from tennis_dry_run import scrape_completed_results
            completed = scrape_completed_results(target_date=today)
        except Exception as exc:
            log.warning("Shadow outcome scrape failed: %s", exc)
            completed = []
        shadow_today = resolve_shadow_outcomes(shadow_today, completed)
        try:
            n = append_shadow_outcomes_log(
                shadow_today,
                Path(state_dir) / "shadow_outcomes.jsonl",
                now_utc,
            )
            if n:
                log.info("Appended %d row(s) to shadow_outcomes.jsonl", n)
        except Exception as exc:
            log.warning("shadow_outcomes.jsonl append failed: %s", exc)

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=today)

    # Build EOD section
    eod_section = "\n".join([
        "",
        f"## EOD Performance — {today_iso}",
        "",
        f"EOD run timestamp: {now_utc.replace(microsecond=0).isoformat()}",
        "",
        render_portfolio_block(replay, now_utc),
        render_today_placer_activity_block(placed_today, placer_skips_today, replay),
        render_today_settlements_block(settled_today, placed_lookup, replay=replay),
        render_stale_carryover_block(pending, placer_skips_today, settled_today, now_utc),
        render_shadow_picks_block(shadow_today),
        render_placer_rejection_diagnostics_block(all_placer_skips, placed, now_utc),
        "---",
        f"_Generated by `tennis_eod_report.py` at "
        f"{now_utc.strftime('%H:%M')} UTC._",
        "",
    ])

    # Append/replace EOD section in daily file
    report_path = Path(vault_dir) / f"{today_iso}.md"
    if not report_path.exists():
        stub = (
            f"---\n"
            f"date: {today_iso}\n"
            f"type: tennis-daily-report\n"
            f"tags: [tennis, dry-run]\n"
            f"---\n\n"
            f"# Tennis Daily Report — {today_iso}\n\n"
            f"_No morning identifier report was generated for this date._\n"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(stub, encoding="utf-8")

    body = report_path.read_text(encoding="utf-8")
    eod_marker = f"## EOD Performance — {today_iso}"
    if eod_marker in body:
        idx = body.index(eod_marker)
        line_start = body.rfind("\n", 0, idx) + 1
        body = body[:line_start].rstrip() + "\n"
    body = body.rstrip() + "\n" + eod_section
    report_path.write_text(body, encoding="utf-8")

    # Re-render rolling file
    if rolling_path is not None:
        open_picks = state.get("open_picks", {}) or {}
        rolling_lines = [
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
            f"_Generated by `tennis_eod_report.py` at "
            f"{now_utc.strftime('%H:%M')} UTC._",
            "",
        ]
        rolling_path = Path(rolling_path)
        rolling_path.parent.mkdir(parents=True, exist_ok=True)
        rolling_path.write_text("\n".join(rolling_lines), encoding="utf-8")

    return report_path


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    now_utc = datetime.now(timezone.utc)

    state_dir = STATE_DIR
    vault_dir = Path(os.getenv(
        "OBSIDIAN_VAULT_DIR",
        "/opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports",
    ))
    rolling_path_env = os.getenv(
        "TENNIS_ROLLING_REPORT",
        "/opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Tennis-Dry-Run-Report.md",
    )
    rolling_path = Path(rolling_path_env) if rolling_path_env else None

    try:
        report_path = write_eod_report(
            now_utc=now_utc,
            state_dir=state_dir,
            vault_dir=vault_dir,
            rolling_path=rolling_path,
        )
        log.info("EOD report written: %s", report_path)
        if rolling_path:
            log.info("Rolling report refreshed: %s", rolling_path)
    except Exception as exc:
        log.exception("EOD vault write failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
