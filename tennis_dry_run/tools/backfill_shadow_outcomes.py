"""Backfill shadow_outcomes.jsonl from historical Daily-Reports markdown.

Reads `## Shadow Picks (tier B, 70-80% — not placed)` tables from each
`YYYY-MM-DD.md` daily report, extracts resolved rows (WIN / LOSS / RETIRED),
and appends them to `shadow_outcomes.jsonl` with `(pick_id, status)` dedup.

`pick_id` is reconstructed deterministically from `(game_date, pick, opponent)`
with a `bf_` prefix so backfilled rows are visibly distinct from live IDs.

Idempotent — safe to re-run.

Usage:
    python tools/backfill_shadow_outcomes.py \\
        --vault-dir /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports \\
        --log-file /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

SHADOW_HEADER = "## Shadow Picks (tier B, 70-80% — not placed)"
NO_PICKS_MARKER = "_No shadow (tier B) picks today._"


def _parse_money(s: str) -> float:
    """Parse '$+7.25' or '$-25.00' or '—' → float (— → 0.0)."""
    s = s.strip()
    if s in ("—", "-", ""):
        return 0.0
    s = s.replace("$", "").replace("+", "").replace(",", "")
    return float(s)


def _stable_pick_id(game_date: date, pick: str, opponent: str) -> str:
    """Deterministic pick_id for backfilled rows. bf_ prefix marks origin."""
    h = hashlib.sha1(f"{game_date.isoformat()}|{pick}|{opponent}".encode("utf-8")).hexdigest()
    return f"bf_{h[:16]}"


def _parse_section(section: str, game_date: date) -> list[dict]:
    """Parse one shadow-picks section body. Returns rows where Outcome ∈
    {WIN, LOSS, RETIRED}; empty if the section has no Outcome column."""
    if NO_PICKS_MARKER in section:
        return []
    header_match = re.search(r"^\s*\|\s*Pick\s*\|.*\|\s*$", section, re.MULTILINE)
    if not header_match:
        return []
    header_line = header_match.group(0).strip()
    headers = [h.strip() for h in header_line.strip("|").split("|")]
    try:
        idx = {
            "pick": headers.index("Pick"),
            "opponent": headers.index("Opponent"),
            "league": headers.index("League"),
            "surface": headers.index("Surface"),
            "model_prob": headers.index("Model Prob"),
            "fair_odds": headers.index("Fair Odds"),
            "match_time": headers.index("Match (UTC)"),
            "outcome": headers.index("Outcome"),
            "pnl": headers.index("Theo PnL"),
        }
    except ValueError:
        return []

    lines = section.splitlines()
    header_pos = next(i for i, ln in enumerate(lines) if ln.strip() == header_line)
    out: list[dict] = []
    for ln in lines[header_pos + 2:]:
        ln = ln.strip()
        if not ln.startswith("|"):
            break
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < len(headers):
            continue
        status = cells[idx["outcome"]]
        if status not in ("WIN", "LOSS", "RETIRED"):
            continue
        pick = cells[idx["pick"]]
        opponent = cells[idx["opponent"]]
        match_time = cells[idx["match_time"]]
        try:
            mt_dt = datetime.strptime(match_time, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            row = {
                "pick_id": _stable_pick_id(game_date, pick, opponent),
                "pick": pick,
                "opponent": opponent,
                "league": cells[idx["league"]],
                "surface": cells[idx["surface"]],
                "model_prob": float(cells[idx["model_prob"]]),
                "fair_odds": float(cells[idx["fair_odds"]]),
                "tier": "B",
                "game_time": int(mt_dt.timestamp()),
                "game_time_iso": mt_dt.isoformat(),
                "status": status,
                "theoretical_pnl": _parse_money(cells[idx["pnl"]]),
                "result_winner": None,
                "resolved_at": f"{game_date.isoformat()}T22:00:00+00:00",
                "backfilled": True,
            }
        except (ValueError, KeyError):
            continue
        out.append(row)
    return out


def parse_shadow_table(md: str, game_date: date) -> list[dict]:
    """Extract resolved shadow rows from a daily-report markdown body.

    Real daily reports carry TWO Shadow Picks sections: the BOD one (header
    only — no Outcome / Theo PnL) and the EOD one (with Outcome / Theo PnL).
    We walk every occurrence and aggregate, deduping by (pick_id, status) so
    a section without Outcome is silently skipped while the resolved one
    contributes rows.
    """
    pos = 0
    aggregated: dict = {}
    while True:
        i = md.find(SHADOW_HEADER, pos)
        if i == -1:
            break
        rest = md[i + len(SHADOW_HEADER):]
        next_heading = rest.find("\n## ")
        section = rest if next_heading == -1 else rest[:next_heading]
        for row in _parse_section(section, game_date):
            aggregated[(row["pick_id"], row["status"])] = row
        pos = i + len(SHADOW_HEADER)
    return list(aggregated.values())


def backfill_directory(vault_dir: Path, log_file: Path) -> int:
    """Process every YYYY-MM-DD.md in vault_dir, append new rows to log_file.

    Returns the number of rows newly appended (0 on a re-run with no new data).
    """
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

    date_re = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
    appended = 0
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        for md_path in sorted(vault_dir.iterdir()):
            m = date_re.match(md_path.name)
            if not m:
                continue
            gd = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            body = md_path.read_text(encoding="utf-8")
            for row in parse_shadow_table(body, gd):
                key = (row["pick_id"], row["status"])
                if key in existing_keys:
                    continue
                f.write(json.dumps(row) + "\n")
                existing_keys.add(key)
                appended += 1
    return appended


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-dir", required=True, type=Path,
                        help="Daily-Reports directory containing YYYY-MM-DD.md files")
    parser.add_argument("--log-file", required=True, type=Path,
                        help="Output shadow_outcomes.jsonl path")
    args = parser.parse_args(argv)
    n = backfill_directory(args.vault_dir, args.log_file)
    print(f"Appended {n} new row(s) to {args.log_file}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
