"""Tests for backfill_shadow_outcomes — parses historical daily-report
markdown to seed shadow_outcomes.jsonl with pre-log outcomes."""

import json
from datetime import date
from pathlib import Path

import pytest


SAMPLE_DAILY_REPORT = """---
date: 2026-05-12
type: tennis-daily-report
tags: [tennis, dry-run]
---

# Tennis Daily Report — 2026-05-12

_some morning content here_

## Shadow Picks (tier B, 70-80% — not placed)

_Theoretical PnL = $25 stake × (fair_odds − 1) per win, −$25 per loss._

| Pick | Opponent | League | Surface | Model Prob | Fair Odds | Match (UTC) | Outcome | Theo PnL |
|---|---|---|---|---:|---:|---|---|---:|
| Linda Noskova | Sara Errani | WTA Rome | clay | 0.7400 | 1.351 | 2026-05-12 09:00 | LOSS | $-25.00 |
| Brandon Nakashima | Alex De Minaur | ATP Rome | hard | 0.7700 | 1.299 | 2026-05-12 10:10 | WIN | $+7.48 |
| Ben Shelton | Basilashvili | ATP Rome | clay | 0.7753 | 1.290 | 2026-05-12 14:20 | WIN | $+7.25 |
| Aryna Sabalenka | Linette | WTA Rome | clay | 0.7400 | 1.351 | 2026-05-12 16:00 | LOSS | $-25.00 |

**Resolved: 4 | Wins: 2 | Win rate: 50.0% | Theoretical PnL: $-35.27**

## EOD Performance — 2026-05-12

_other eod content_
"""


def test_parse_shadow_table_extracts_resolved_rows():
    """parse_shadow_table returns one dict per WIN/LOSS/RETIRED row."""
    from tools.backfill_shadow_outcomes import parse_shadow_table

    rows = parse_shadow_table(SAMPLE_DAILY_REPORT, date(2026, 5, 12))

    assert len(rows) == 4
    by_pick = {r["pick"]: r for r in rows}
    assert by_pick["Linda Noskova"]["status"] == "LOSS"
    assert by_pick["Linda Noskova"]["theoretical_pnl"] == pytest.approx(-25.0)
    assert by_pick["Linda Noskova"]["model_prob"] == pytest.approx(0.74)
    assert by_pick["Linda Noskova"]["fair_odds"] == pytest.approx(1.351)
    assert by_pick["Linda Noskova"]["opponent"] == "Sara Errani"
    assert by_pick["Linda Noskova"]["league"] == "WTA Rome"
    assert by_pick["Linda Noskova"]["surface"] == "clay"
    assert by_pick["Linda Noskova"]["game_time_iso"] == "2026-05-12T09:00:00+00:00"
    assert by_pick["Brandon Nakashima"]["status"] == "WIN"
    assert by_pick["Brandon Nakashima"]["theoretical_pnl"] == pytest.approx(7.48)
    # pick_id should be deterministic + namespaced.
    assert by_pick["Linda Noskova"]["pick_id"].startswith("bf_")
    assert by_pick["Linda Noskova"]["pick_id"] != by_pick["Brandon Nakashima"]["pick_id"]


def test_parse_shadow_table_skips_pending_rows():
    """Pending rows are excluded — they'll be picked up by the live writer."""
    from tools.backfill_shadow_outcomes import parse_shadow_table

    md = SAMPLE_DAILY_REPORT.replace(
        "| Linda Noskova | Sara Errani | WTA Rome | clay | 0.7400 | 1.351 | 2026-05-12 09:00 | LOSS | $-25.00 |",
        "| Future Pick | X | ATP Rome | clay | 0.7400 | 1.351 | 2026-05-12 23:00 | pending | — |",
    )

    rows = parse_shadow_table(md, date(2026, 5, 12))

    pick_names = {r["pick"] for r in rows}
    assert "Future Pick" not in pick_names
    assert len(rows) == 3


def test_parse_shadow_table_handles_no_shadow_section():
    """A daily report without a Shadow Picks section returns []."""
    from tools.backfill_shadow_outcomes import parse_shadow_table

    rows = parse_shadow_table("# Tennis Daily Report — 2026-05-12\n\n_empty_\n", date(2026, 5, 12))
    assert rows == []


def test_parse_shadow_table_handles_no_picks_today():
    """The 'No shadow (tier B) picks today.' marker returns []."""
    from tools.backfill_shadow_outcomes import parse_shadow_table

    md = "# X\n\n## Shadow Picks (tier B, 70-80% — not placed)\n\n_No shadow (tier B) picks today._\n"
    rows = parse_shadow_table(md, date(2026, 5, 12))
    assert rows == []


def test_parse_shadow_table_handles_t90_columns():
    """Reports with T-90 columns mid-table still parse correctly."""
    from tools.backfill_shadow_outcomes import parse_shadow_table

    md = """# Tennis Daily Report — 2026-05-10

## Shadow Picks (tier B, 70-80% — not placed)

| Pick | Opponent | League | Surface | Model Prob | Fair Odds | Match (UTC) | T-90 result | T-90 odds | Outcome | Theo PnL |
|---|---|---|---|---:|---:|---|---|---:|---|---:|
| Ben Shelton | Basilashvili | ATP Rome | clay | 0.7753 | 1.290 | 2026-05-10 14:20 | would_place | 3.310 | WIN | $+7.25 |

**Resolved: 1**
"""
    rows = parse_shadow_table(md, date(2026, 5, 10))
    assert len(rows) == 1
    assert rows[0]["pick"] == "Ben Shelton"
    assert rows[0]["status"] == "WIN"
    assert rows[0]["theoretical_pnl"] == pytest.approx(7.25)


def test_backfill_writes_dedupes_against_existing_log(tmp_path):
    """Running backfill twice appends each row once. (pick_id, status) dedup."""
    from tools.backfill_shadow_outcomes import backfill_directory

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2026-05-12.md").write_text(SAMPLE_DAILY_REPORT, encoding="utf-8")

    log_file = tmp_path / "shadow_outcomes.jsonl"

    n1 = backfill_directory(vault, log_file)
    n2 = backfill_directory(vault, log_file)

    assert n1 == 4
    assert n2 == 0
    rows = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 4


def test_backfill_processes_multiple_daily_reports(tmp_path):
    """All YYYY-MM-DD.md files in the directory are processed."""
    from tools.backfill_shadow_outcomes import backfill_directory

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2026-05-12.md").write_text(SAMPLE_DAILY_REPORT, encoding="utf-8")
    (vault / "2026-05-13.md").write_text(
        SAMPLE_DAILY_REPORT.replace("2026-05-12", "2026-05-13"),
        encoding="utf-8",
    )
    (vault / "not-a-date.md").write_text("ignored", encoding="utf-8")

    log_file = tmp_path / "shadow_outcomes.jsonl"
    n = backfill_directory(vault, log_file)

    assert n == 8
    rows = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    dates = {r["game_time_iso"][:10] for r in rows}
    assert dates == {"2026-05-12", "2026-05-13"}
