"""Tests for tennis_eod_report — daily EOD performance summary.

Run by cron at 22:00 UTC. Reads state.json + trades.jsonl + skipped.jsonl
+ pending_selections.jsonl, aggregates today's activity, and appends an
"EOD Performance" section to <vault_dir>/YYYY-MM-DD.md (or replaces an
existing EOD section, idempotently).
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import tennis_eod_report as eod


# ── Fixtures ──────────────────────────────────────────────────────────────────

TODAY_DT = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
TODAY = TODAY_DT.date()
YESTERDAY = TODAY - timedelta(days=1)


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def tmp_files(tmp_path):
    state_file = tmp_path / "state.json"
    trades_file = tmp_path / "trades.jsonl"
    skipped_file = tmp_path / "skipped.jsonl"
    pending_file = tmp_path / "pending_selections.jsonl"
    state_file.write_text(json.dumps({
        "balance": 525.50,
        "total_bets": 1,
        "wins": 1,
        "losses": 0,
        "total_pnl": 25.50,
        "open_picks": {"0xopen1": {"pick": "X"}},
        "today_bets": 1,
    }), encoding="utf-8")
    return state_file, trades_file, skipped_file, pending_file


# ── aggregate_eod ─────────────────────────────────────────────────────────────

def test_aggregate_eod_counts_today_only(tmp_files):
    """Skips and trades from prior days are excluded."""
    state_file, trades_file, skipped_file, pending_file = tmp_files

    today_skip = {"source": "placer", "reason": "no_liquidity",
                  "ts": _ts(TODAY_DT), "pick": "A"}
    yesterday_skip = {"source": "placer", "reason": "no_liquidity",
                      "ts": _ts(TODAY_DT.replace(day=6)), "pick": "B"}
    _write_jsonl(skipped_file, [today_skip, yesterday_skip])

    today_pending = {"pick_id": "p1", "pick": "A", "ts": _ts(TODAY_DT)}
    yesterday_pending = {"pick_id": "p0", "pick": "Z", "ts": _ts(TODAY_DT.replace(day=6))}
    _write_jsonl(pending_file, [today_pending, yesterday_pending])

    agg = eod.aggregate_eod(TODAY, state_file, trades_file, skipped_file, pending_file)

    assert agg["selections_identified"] == 1
    assert agg["skipped_at_placement"] == 1


def test_aggregate_eod_separates_placer_skips_from_scan_skips(tmp_files):
    """Skips with source=placer count as placer; everything else is scan-loop."""
    state_file, trades_file, skipped_file, pending_file = tmp_files
    rows = [
        {"source": "placer", "reason": "no_liquidity", "ts": _ts(TODAY_DT)},
        {"source": "placer", "reason": "odds_out_of_range_at_placement",
         "ts": _ts(TODAY_DT)},
        {"reason": "odds_out_of_range", "ts": _ts(TODAY_DT)},  # scan loop (no source)
        {"reason": "low_confidence", "ts": _ts(TODAY_DT)},
    ]
    _write_jsonl(skipped_file, rows)

    agg = eod.aggregate_eod(TODAY, state_file, trades_file, skipped_file, pending_file)

    assert agg["skipped_at_placement"] == 2
    assert len(agg["scan_skips"]) == 2


def test_aggregate_eod_counts_placements_and_settlements(tmp_files):
    """trades.jsonl carries `type: open` / `type: settled` rows."""
    state_file, trades_file, skipped_file, pending_file = tmp_files
    rows = [
        {"type": "open", "pick_id": "0xa", "pick": "Player A",
         "sxbet_odds": 1.45, "ts": _ts(TODAY_DT)},
        {"type": "settled", "pick_id": "0xprev", "outcome": "win",
         "pnl": 25.50, "ts": _ts(TODAY_DT)},
        {"type": "settled", "pick_id": "0xprev2", "outcome": "loss",
         "pnl": -25.0, "ts": _ts(TODAY_DT)},
        {"type": "open", "pick_id": "0xy", "pick": "Yesterday",
         "ts": _ts(TODAY_DT.replace(day=6))},  # excluded
    ]
    _write_jsonl(trades_file, rows)

    agg = eod.aggregate_eod(TODAY, state_file, trades_file, skipped_file, pending_file)

    assert agg["placed"] == 1
    assert agg["settled_today"] == 2
    assert agg["realised_pnl_today"] == pytest.approx(0.50)


def test_aggregate_eod_handles_missing_files(tmp_path):
    """No journals at all → all-zeros aggregation, no crash."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"balance": 500.0, "open_picks": {}}),
                          encoding="utf-8")

    agg = eod.aggregate_eod(
        TODAY, state_file,
        tmp_path / "missing_trades.jsonl",
        tmp_path / "missing_skipped.jsonl",
        tmp_path / "missing_pending.jsonl",
    )

    assert agg["placed"] == 0
    assert agg["skipped_at_placement"] == 0
    assert agg["settled_today"] == 0
    assert agg["selections_identified"] == 0
    assert agg["cumulative_balance"] == 500.0


# ── render_eod_section ────────────────────────────────────────────────────────

def test_render_eod_section_includes_summary_table():
    """Rendered markdown contains the canonical metrics row labels."""
    agg = {
        "today": "2026-05-07",
        "selections_identified": 5, "placer_fires": 5, "placed": 0,
        "skipped_at_placement": 5, "settled_today": 0,
        "realised_pnl_today": 0.0, "open_picks_count": 0,
        "cumulative_balance": 500.0,
        "placer_skips": [], "placed_orders": [], "scan_skips": [],
    }

    section = eod.render_eod_section(TODAY_DT, agg)

    assert "## EOD Performance — 2026-05-07" in section
    assert "Selections identified" in section
    assert "Cumulative balance" in section
    assert "$500.00" in section


def test_render_eod_section_no_activity_message():
    """When there are no placer skips and no placements, render an explicit
    'No placer activity today' line (not an empty table)."""
    agg = {
        "today": "2026-05-07",
        "selections_identified": 0, "placer_fires": 0, "placed": 0,
        "skipped_at_placement": 0, "settled_today": 0,
        "realised_pnl_today": 0.0, "open_picks_count": 0,
        "cumulative_balance": 500.0,
        "placer_skips": [], "placed_orders": [], "scan_skips": [],
    }
    section = eod.render_eod_section(TODAY_DT, agg)
    assert "No placer activity today" in section


# ── upsert_eod_section ────────────────────────────────────────────────────────

MORNING_REPORT = (
    "---\n"
    "date: 2026-05-07\n"
    "type: identifier-report\n"
    "tags: [tennis, identifier, dry-run]\n"
    "---\n\n"
    "# Tennis Identifier Report — 2026-05-07\n\n"
    "Run timestamp: 2026-05-07T07:00:00+00:00\n\n"
    "## Summary\n\n"
    "| Metric | Value |\n"
    "|---|---|\n"
    "| Markets total | 88 |\n"
)


def test_upsert_eod_section_appends_to_existing_morning_report(tmp_path):
    report_path = tmp_path / "2026-05-07.md"
    report_path.write_text(MORNING_REPORT, encoding="utf-8")

    eod_section = "\n## EOD Performance — 2026-05-07\n\n_test_\n"
    eod.upsert_eod_section(report_path, eod_section, TODAY)

    body = report_path.read_text(encoding="utf-8")
    assert "Markets total" in body  # morning report preserved
    assert "## EOD Performance — 2026-05-07" in body
    assert body.count("## EOD Performance — 2026-05-07") == 1


def test_upsert_eod_section_creates_stub_when_no_morning_report(tmp_path):
    """If identifier didn't run, create a minimal report file."""
    report_path = tmp_path / "2026-05-07.md"
    eod_section = "\n## EOD Performance — 2026-05-07\n\n_test_\n"
    eod.upsert_eod_section(report_path, eod_section, TODAY)

    assert report_path.exists()
    body = report_path.read_text(encoding="utf-8")
    assert "## EOD Performance — 2026-05-07" in body
    assert "type: identifier-report" in body  # stub still has frontmatter


def test_upsert_eod_section_replaces_existing_section_on_rerun(tmp_path):
    """Idempotency: running twice keeps exactly one EOD section, with the
    later content."""
    report_path = tmp_path / "2026-05-07.md"
    report_path.write_text(MORNING_REPORT, encoding="utf-8")

    first = "\n## EOD Performance — 2026-05-07\n\nplaced=0 first run\n"
    eod.upsert_eod_section(report_path, first, TODAY)

    second = "\n## EOD Performance — 2026-05-07\n\nplaced=2 second run\n"
    eod.upsert_eod_section(report_path, second, TODAY)

    body = report_path.read_text(encoding="utf-8")
    assert body.count("## EOD Performance — 2026-05-07") == 1
    assert "second run" in body
    assert "first run" not in body
    assert "Markets total" in body  # morning report still intact
