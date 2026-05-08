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
from collections import Counter
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tennis_dry_run import (  # noqa: E402
    JOURNAL_FILE,
    SKIPPED_FILE,
    STATE_DIR,
    STATE_FILE,
)

log = logging.getLogger("tennis_eod_report")

EOD_SECTION_HEADER = "## EOD Performance"


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


def aggregate_eod(
    today: date,
    state_file: Path,
    trades_file: Path,
    skipped_file: Path,
    pending_file: Path,
) -> dict:
    """Aggregate today's activity from state + journal files."""
    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    trades = [r for r in _read_jsonl(trades_file) if _is_today(r.get("ts", ""), today)]
    skipped = [r for r in _read_jsonl(skipped_file) if _is_today(r.get("ts", ""), today)]
    pending_today = [
        r for r in _read_jsonl(pending_file) if _is_today(r.get("ts", ""), today)
    ]

    placed_orders = [t for t in trades if t.get("type") == "open"]
    settled = [t for t in trades if t.get("type") == "settled"]
    realised_pnl = sum(float(t.get("pnl", 0.0)) for t in settled)

    placer_skips = [s for s in skipped if s.get("source") == "placer"]
    scan_skips = [s for s in skipped if s.get("source") != "placer"]

    return {
        "today": today.isoformat(),
        "selections_identified": len(pending_today),
        "placer_fires": len(placer_skips) + len(placed_orders),
        "placed": len(placed_orders),
        "skipped_at_placement": len(placer_skips),
        "settled_today": len(settled),
        "realised_pnl_today": round(realised_pnl, 2),
        "open_picks_count": len(state.get("open_picks", {}) or {}),
        "cumulative_balance": float(state.get("balance", 0.0)),
        "placer_skips": placer_skips,
        "scan_skips": scan_skips,
        "placed_orders": placed_orders,
        "pending_today": pending_today,
    }


def render_eod_section(now_utc: datetime, agg: dict) -> str:
    """Render the EOD section as a markdown string. Idempotent given input."""
    lines = [
        "",
        f"{EOD_SECTION_HEADER} — {agg['today']}",
        "",
        f"Run timestamp: {now_utc.replace(microsecond=0).isoformat()}",
        "",
        "### Outcomes",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Selections identified | {agg['selections_identified']} |",
        f"| Placer fires | {agg['placer_fires']} |",
        f"| Placed | {agg['placed']} |",
        f"| Skipped at placement | {agg['skipped_at_placement']} |",
        f"| Settled today | {agg['settled_today']} |",
        f"| Realised PnL today | ${agg['realised_pnl_today']:.2f} |",
        f"| Open picks (carry) | {agg['open_picks_count']} |",
        f"| Cumulative balance | ${agg['cumulative_balance']:.2f} |",
        "",
        "### Placer detail",
        "",
    ]
    if not agg["placer_skips"] and not agg["placed_orders"]:
        lines.append("_No placer activity today._")
    else:
        lines.append("| Pick | Opponent | League | SX Bet odds | Result |")
        lines.append("|---|---|---|---:|---|")
        for o in agg["placed_orders"]:
            odds = o.get("sxbet_odds")
            odds_str = f"{float(odds):.3f}" if isinstance(odds, (int, float)) else "—"
            lines.append(
                f"| {o.get('pick','?')} | {o.get('opponent','?')} | "
                f"{o.get('league','?')} | {odds_str} | placed ({o.get('mode','?')}) |"
            )
        for s in agg["placer_skips"]:
            odds = s.get("sxbet_odds")
            odds_str = f"{float(odds):.3f}" if isinstance(odds, (int, float)) else "—"
            lines.append(
                f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
                f"{s.get('league','?')} | {odds_str} | skipped: {s.get('reason','?')} |"
            )

    lines += [
        "",
        "### Scan-loop daemon (parallel track)",
        "",
    ]
    if agg["scan_skips"]:
        c = Counter(s.get("reason", "?") for s in agg["scan_skips"])
        for reason, n in c.most_common():
            lines.append(f"- `{reason}` x {n}")
    else:
        lines.append("_No scan-loop skips today._")

    lines += [
        "",
        "---",
        f"_Generated by `tennis_eod_report.py` at "
        f"{now_utc.strftime('%H:%M')} UTC._",
        "",
    ]

    return "\n".join(lines)


def upsert_eod_section(report_path: Path, eod_section: str, today: date) -> None:
    """Append or replace the EOD section in today's report file.

    If no morning report exists, write a minimal stub frontmatter + header
    first. If an EOD section is already present (rerun), drop it and
    re-append with the new content.
    """
    today_iso = today.isoformat()
    if not report_path.exists():
        stub = (
            f"---\n"
            f"date: {today_iso}\n"
            f"type: identifier-report\n"
            f"tags: [tennis, identifier, dry-run]\n"
            f"---\n\n"
            f"# Tennis Identifier Report — {today_iso}\n\n"
            f"_No morning identifier report was generated for this date._\n"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(stub, encoding="utf-8")

    body = report_path.read_text(encoding="utf-8")
    eod_marker = f"{EOD_SECTION_HEADER} — {today_iso}"

    if eod_marker in body:
        idx = body.index(eod_marker)
        line_start = body.rfind("\n", 0, idx) + 1
        body = body[:line_start].rstrip() + "\n"

    body = body.rstrip() + "\n" + eod_section
    report_path.write_text(body, encoding="utf-8")


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    today = _today_utc()
    now_utc = datetime.now(timezone.utc)

    state_file = Path(os.getenv("STATE_FILE", str(STATE_FILE)))
    trades_file = Path(os.getenv("TRADES_FILE", str(JOURNAL_FILE)))
    skipped_file = Path(os.getenv("SKIPPED_FILE_PATH", str(SKIPPED_FILE)))
    pending_file = Path(os.getenv(
        "PENDING_SELECTIONS_FILE",
        str(STATE_DIR / "pending_selections.jsonl"),
    ))
    vault_dir = Path(os.getenv(
        "OBSIDIAN_VAULT_DIR",
        "/opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports",
    ))

    agg = aggregate_eod(today, state_file, trades_file, skipped_file, pending_file)
    section = render_eod_section(now_utc, agg)

    report_path = vault_dir / f"{today.isoformat()}.md"
    try:
        upsert_eod_section(report_path, section, today)
        log.info(
            "EOD report written: %s (placed=%d skipped=%d settled=%d pnl=$%.2f)",
            report_path, agg["placed"], agg["skipped_at_placement"],
            agg["settled_today"], agg["realised_pnl_today"],
        )
    except Exception as exc:
        log.exception("EOD vault write failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
