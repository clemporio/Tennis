# Tennis Pipeline Retirement + Reporting Polish Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the residuals from the 2026-05-13 reporting-accuracy remediation and execute Option 1 of the pipeline reconciliation spec (retire `run_scan`; identifier+placer becomes the sole pick-creation path).

**Architecture:** All changes are local to existing modules in `tennis_dry_run/`. No new dependencies. The bot service's `main()` loop is restructured from "scan + settle + save" to "settle + save" only. Scan/place duties are fully delegated to the cron-driven identifier+placer pipeline. Tests are written first, then the production change, then a VPS deploy with explicit rollback points.

**Tech Stack:** Python 3.13, pytest 9, BeautifulSoup4. No new runtime deps.

**Context — what already shipped 2026-05-13:**
- 14-task reporting-accuracy plan complete: commits `1154909..c692c62` on `main`. See `docs/superpowers/plans/2026-05-13-tennis-reporting-accuracy.md`.
- VPS state: balance $475.61, total_pnl -$24.39, w/l 3/2, `applied_corrections` marker set, service active.
- Test suite: 186 passed, 1 skipped (Windows-only flock concurrency test).
- The dual-pipeline reconciliation spec at [`docs/superpowers/specs/2026-05-13-tennis-pipeline-reconciliation.md`](../specs/2026-05-13-tennis-pipeline-reconciliation.md) was authored as Task 12. **User chose Option 1: retire `run_scan`.**

**Reference for the spec findings this plan addresses:** see `docs/superpowers/specs/2026-05-13-tennis-pipeline-reconciliation.md`, particularly the 11 enumerated differences between the two pipelines.

---

## File structure

**Create:**
- `tennis_dry_run/tests/test_settle_only_main.py` — new tests for the settle-only main loop (Task H).
- `tennis_dry_run/tests/test_run_scan_excludes_challengers.py` — interim SEV-2 mitigation test (Task C); deleted in Task H.

**Modify:**
- `tennis_dry_run/tennis_kelly.py` — round replay balance/total_pnl (Task B).
- `tennis_dry_run/tennis_dry_run.py` — interim Challenger exclusion (Task C), then full removal of `run_scan` from `main()` (Task H). The `run_scan` function itself remains importable as a deprecated no-op for one release cycle.
- `tennis_dry_run/tennis_placer.py` — write `source: "identifier_placer"` field on every journal open row (Task G).
- `tennis_dry_run/tennis_identifier.py` — enforce daily cap (Task F).
- Affected tests under `tennis_dry_run/tests/test_*.py`.

**No changes to:** `tennis_eod_report.py`, `tennis_portfolio.py`, `tennis_signing.py`, `tennis_sxbet.py`, `tennis_shadow_placer.py`, `tools/correct_settlements.py`.

**Deleted at end of plan:** none (run_scan function preserved as no-op for one release; remove in the cycle after).

---

## Phase 1 — Close out the unreviewed work from 2026-05-13

### Task A: Spec + code-quality review of commit `c692c62`

**Why:** During the 2026-05-13 deploy, the renderers were found to ignore `settled_correction` overrides (Yesterday's Results rendered pre-correction values). Fix landed as commit `c692c62` (`_apply_settled_corrections` helper piped into identifier + eod). The fix was dispatched without the spec/quality review loop because deployment was time-pressured.

**Files inspected:**
- `tennis_dry_run/tennis_dry_run.py:336-364` (helper)
- `tennis_dry_run/tennis_identifier.py:340-352` (wire-in)
- `tennis_dry_run/tennis_eod_report.py:158-161` (wire-in)
- `tennis_dry_run/tests/test_apply_settled_corrections.py` (4 unit tests)
- `tennis_dry_run/tests/test_tennis_identifier.py` (added integration test)

- [ ] **Step 1: Read the helper and the two wire-in sites.** Verify:
  - Helper removes `settled_correction` rows from output (not just inert).
  - Override applies `outcome`, `pnl`, `result_winner`; preserves all other fields.
  - `corrected=True` flag is set on overridden rows for traceability.
  - Wire-in happens BEFORE the placed/settled split in both identifier and eod.

- [ ] **Step 2: Search for any THIRD journal-load site that may have been missed.**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas"
grep -rn "trades\.jsonl\|JOURNAL_FILE.*read\|_read_jsonl" tennis_dry_run/*.py
```

For each match: confirm it either (a) routes through `_apply_settled_corrections`, or (b) doesn't feed into a renderer/replay (e.g., the correction-write side in `tools/correct_settlements.py` is correctly NOT routed).

- [ ] **Step 3: Edge-case review.** Verify the helper handles:
  - Multiple corrections for different `settled` rows.
  - A `settled_correction` with no matching `settled` (dangling `corrects`). Expected: row dropped silently (it's `settled_correction`, never re-added). Confirm in the helper.
  - A correction-of-correction (`settled_correction` for another `settled_correction`). Out of scope — the journal model doesn't allow this; verify the helper doesn't crash.
  - Empty journal. Helper should return empty list.

- [ ] **Step 4: Run the 4 helper tests + 1 integration test.**

```
cd tennis_dry_run
python -m pytest tests/test_apply_settled_corrections.py tests/test_tennis_identifier.py::test_write_daily_report_applies_settled_corrections -v
```
Expected: 5 passed.

- [ ] **Step 5: If issues found, fix and commit as a follow-up.** Otherwise mark this task complete — no code change.

---

## Phase 2 — Polish flagged during the 2026-05-13 reviews

### Task B: Round balance/total_pnl in Kelly replay

**Why:** `tennis_kelly.py:212-213` accumulates `ms["balance"] += pnl` and `ms["total_pnl"] += pnl` unrounded. Task 4 of the reporting plan rounded the `state.json` write path but missed the replay-time accumulation. Display drift can grow over many settles, mirroring the bug Task 4 closed on the bot side.

**Files:**
- Modify: `tennis_dry_run/tennis_kelly.py` (the settled branch in `replay_three_bankrolls`).
- Test: `tennis_dry_run/tests/test_tennis_kelly_rounding.py` (new file).

- [ ] **Step 1: Write the failing test**

Create `tennis_dry_run/tests/test_tennis_kelly_rounding.py`:

```python
"""Replay balance/total_pnl must round to journal precision on each settle."""

from __future__ import annotations

from datetime import date

import pytest

from tennis_kelly import replay_three_bankrolls


def test_replay_balance_rounds_to_two_decimals():
    """A pnl of 8.333... must accumulate as 8.33 in the replay's running balance."""
    placed = [{
        "type": "open", "pick_id": "p1", "ts": "2026-05-12T07:00:00+00:00",
        "model_prob": 0.85, "sxbet_odds": 1.3333333333,
        "sxbet_available_usd": 500.0, "stake": 25.0,
    }]
    settled = [{
        "type": "settled", "pick_id": "p1", "ts": "2026-05-12T15:00:00+00:00",
        "outcome": "win", "sxbet_odds": 1.3333333333, "stake": 25.0,
        "pnl": 8.33,  # journal-rounded
    }]

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=date(2026, 5, 13))

    # Balance must equal 500 + journal pnl, not 500 + unrounded pnl.
    assert replay["base"]["balance"] == pytest.approx(508.33, abs=1e-9), \
        f"replay balance {replay['base']['balance']} drifted from journal sum 508.33"
    # And after rounding, no float artifacts should leak: balance must be
    # exactly representable as 2dp (≡ × 100 is integer).
    assert round(replay["base"]["balance"] * 100) / 100.0 == replay["base"]["balance"]
```

- [ ] **Step 2: Run, confirm failure**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -m pytest tests/test_tennis_kelly_rounding.py -v
```
Expected: FAIL with drift (`508.3299999...` or similar) — replay accumulator uses unrounded pnl.

- [ ] **Step 3: Apply the rounding fix**

In `tennis_dry_run/tennis_kelly.py`, find the settled branch in `replay_three_bankrolls` (the block that pops `open_stakes[pick_id]` and increments `ms["balance"]` / `ms["total_pnl"]`). Replace:

```python
                ms["balance"] += pnl
                ms["total_pnl"] += pnl
```

with:

```python
                ms["balance"] = round(ms["balance"] + pnl, 2)
                ms["total_pnl"] = round(ms["total_pnl"] + pnl, 2)
```

**Note:** verify by reading lines 200–225 first; do not blindly paste.

- [ ] **Step 4: Run test, confirm pass**

```
python -m pytest tests/test_tennis_kelly_rounding.py -v
```

- [ ] **Step 5: Run full Kelly suite — no regression**

```
python -m pytest tests/test_tennis_kelly.py tests/test_tennis_kelly_deployed.py tests/test_tennis_kelly_rounding.py -v
```

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -q
```
Expected: 187 passed (186 + 1 new), 1 skipped.

- [ ] **Step 7: Commit**

```
git add tennis_dry_run/tennis_kelly.py tennis_dry_run/tests/test_tennis_kelly_rounding.py
git commit -m "fix(tennis_kelly): round replay balance/total_pnl to journal precision per settle"
```

---

### Task C: Interim SEV-2 mitigation — `run_scan` must respect Challenger/qualifying/ITF exclusion

**Why:** Task 12 spec finding #2: `run_scan` ignores the league-exclusion check that `evaluate_market` enforces. The bot has been opening picks in tournaments the user has explicitly banned (Challenger, qualifying, ITF, qualif., q1/q2/q3). This is a SEV-2 bug, not just reporting drift.

**This is interim** — Task H (retire `run_scan`) supersedes it. But until retirement ships, every scan day the bot can place a Challenger pick. Mitigate now.

**Files:**
- Modify: `tennis_dry_run/tennis_dry_run.py:run_scan`.
- Test: `tennis_dry_run/tests/test_run_scan_excludes_challengers.py` (new file; deleted in Task H).

- [ ] **Step 1: Locate the league filter in identifier**

Find the league-exclusion predicate in `tennis_identifier.py` (it's the function or inline check that rejects markets where league name contains any of `("challenger", "qualifying", "qualif.", " q1", " q2", " q3", "itf ")`). Note the exact name list and matching logic (case-insensitive substring match).

- [ ] **Step 2: Write the failing test**

Create `tennis_dry_run/tests/test_run_scan_excludes_challengers.py`:

```python
"""run_scan must reject Challenger / qualifying / ITF markets (matches identifier rule)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("league", [
    "ATP Challenger Madrid", "WTA 125 Challenger", "ATP Rome Qualifying",
    "Davis Cup Qualif.", "ITF M25 Heraklion", "ATP Rome Q1",
])
def test_run_scan_rejects_excluded_leagues(league, monkeypatch, tmp_path):
    """A market whose league matches the exclusion list must be skipped by run_scan.

    Stub out scrape_scheduled_matches to return a single market in `league`.
    Stub the predictor to be a happy-path returner. Run run_scan against a
    fresh state. Assert the state.open_picks remains empty and the journal
    has no `open` row.
    """
    import json
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")

    market = {
        "market_hash": "0xdeadbeef" + "00" * 28,
        "outcome_one_name": "Player A",
        "outcome_two_name": "Player B",
        "league": league,
        # ... whatever other fields run_scan reads — copy from a happy-path test
    }
    monkeypatch.setattr(tdr, "scrape_scheduled_matches", lambda: [market])

    # Make predictor happy and odds available — anything that would pass
    # without the league filter.
    monkeypatch.setattr(tdr, "predict_winner",
                        lambda *a, **kw: {"winner": "Player A", "prob": 0.9})
    # ... whatever other helpers run_scan touches before/after the league
    # check; stub them too.

    state = {"balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
             "open_picks": {}, "today_bets": 0, "today_date": "2026-05-14"}
    new_state = tdr.run_scan(state)

    assert new_state["open_picks"] == {}, f"Challenger market {league!r} was not excluded"
    journal_path = tmp_path / "trades.jsonl"
    journal = journal_path.read_text() if journal_path.exists() else ""
    assert "\"type\": \"open\"" not in journal, \
        f"journal contains open row for excluded league: {journal}"
```

**Implementation note:** the exact stubs needed depend on `run_scan`'s call flow. Read the function end-to-end before writing the test — you may need to stub `fetch_book`, `sxbet_get_market_book`, etc. The principle: make every gate AFTER the league check pass, so only the league check can cause the rejection.

- [ ] **Step 3: Run, confirm failure**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -m pytest tests/test_run_scan_excludes_challengers.py -v
```
Expected: all 6 parameterised cases FAIL (open_picks non-empty for each).

- [ ] **Step 4: Add the league check to `run_scan`**

In `tennis_dry_run.py:run_scan`, locate the per-market loop. BEFORE any predictor call or stake computation, add:

```python
        # Hard exclusion: bot must never open picks in Challenger / qualifying /
        # ITF leagues. Matches the identifier's rule (tennis_identifier.py).
        league = (market.get("league") or "").lower()
        if any(tag in league for tag in
               ("challenger", "qualifying", "qualif.", " q1", " q2", " q3", "itf ")):
            log.info("run_scan: skipping excluded league %r for market %s",
                     market.get("league"), market.get("market_hash"))
            continue
```

Adapt the exact tag list to match what identifier uses (the identifier may use a frozen set or a module constant — if so, import and reuse it rather than duplicating the literal).

- [ ] **Step 5: Run test, confirm pass**

```
python -m pytest tests/test_run_scan_excludes_challengers.py -v
```

- [ ] **Step 6: Full suite**

```
python -m pytest tests/ -q
```
Expected: 193 passed (187 + 6 new), 1 skipped.

- [ ] **Step 7: Commit**

```
git add tennis_dry_run/tennis_dry_run.py tennis_dry_run/tests/test_run_scan_excludes_challengers.py
git commit -m "fix(tennis): run_scan respects Challenger/qualifying/ITF exclusion (interim, supersedes via Task H retirement)"
```

---

### Task D: Verify state-locking concurrency test on Linux (VPS)

**Why:** Task 9's `update_state_atomic` was implemented with a Windows fallback (`fcntl = None` on win32). The 20-process flock concurrency test (`tests/test_state_locking.py::test_concurrent_writers_no_clobber`) module-level skips on Windows. Production is Linux; the test has never been observed to pass on the platform it was written for.

**Files:**
- No code change. This is verification only.

- [ ] **Step 1: Ensure pytest is installed in the VPS venv**

```bash
ssh vps "cd /opt/tennis-dry-run && venv/bin/python -m pip show pytest >/dev/null 2>&1 && echo OK || venv/bin/python -m pip install pytest pytest-asyncio"
```

- [ ] **Step 2: rsync the test file (and only that file) to VPS**

```bash
scp tennis_dry_run/tests/test_state_locking.py vps:/opt/tennis-dry-run/tests/test_state_locking.py
```

If `/opt/tennis-dry-run/tests/` doesn't exist:

```bash
ssh vps "mkdir -p /opt/tennis-dry-run/tests && touch /opt/tennis-dry-run/tests/__init__.py"
```

- [ ] **Step 3: Run the concurrency test on the VPS against a tmp state file**

```bash
ssh vps "cd /opt/tennis-dry-run && venv/bin/python -m pytest tests/test_state_locking.py -v"
```

Expected: `test_concurrent_writers_no_clobber PASSED` + `test_update_state_atomic_callable_exists PASSED` + (any merge tests from Task 9 also PASSED). If any fail, the locking has a real bug — escalate.

- [ ] **Step 4: Tear down**

The test runs against `tmp_path`, so no production state is touched. Leave the test file on the VPS or remove — caller's choice.

- [ ] **Step 5: Record the result**

Update the memory note `tennis_trading/project_reporting_accuracy_remediation.md` to remove the line "Locking concurrency test never runs on Windows; first real verification will be on the VPS." Replace with "Linux flock concurrency test verified on VPS 2026-MM-DD." No commit needed for memory updates (they're outside the repo).

---

## Phase 3 — Retire `run_scan` (Task 12 spec, Option 1)

### Task E: Audit `run_scan` for any logic NOT in identifier + placer

**Why:** Before removing `run_scan` from `main()`, verify there's no behaviour it provides that identifier+placer doesn't already cover. The Task 12 spec listed 11 differences. Some (league exclusion, today-only filter, liquidity gate) are already in identifier or placer. Others (`MAX_DAILY_BETS` cap, journal `source` field) need explicit migration.

**Files:**
- No code change. This is investigation only; output is a checklist for Tasks F–H.

- [ ] **Step 1: Diff the gate sequences**

Read `tennis_dry_run.py:run_scan` and `tennis_identifier.py:evaluate_market` side-by-side. For each gate in `run_scan`, identify the matching gate in `evaluate_market` (or note its absence).

Expected mapping (from Task 12 spec):

| `run_scan` gate | `evaluate_market` equivalent |
|---|---|
| `if league in EXCLUDED_LEAGUES` | same (already enforced; Task C made bot consistent) |
| `if today_bets >= MAX_DAILY_BETS` | **MISSING — Task F migrates** |
| `if model_prob < MIN_CONFIDENCE (0.80)` | identifier uses tiered 0.70 (shadow) / 0.80 (place); placer enforces tier-A only for live places |
| `if not market.outcome_one_name in players` | identifier uses TE schedule match — equivalent |
| `if liquidity < $25` | placer enforces at T-15 (sharper) |
| `if fair_odds not in [MIN, MAX]` | identifier enforces same; placer re-checks at T-15 |
| `if pick_already_in_open_picks` (dedup) | both enforce |
| `journal "open" row with source field` | identifier+placer doesn't emit `source` — **Task G adds** |

- [ ] **Step 2: List anything NOT covered by Task F/G/H**

Write a paragraph in your task report noting any `run_scan` behaviour that's NOT covered by Tasks F (daily cap), G (source field), or H (retirement). If you find such behaviour, STOP and escalate — the plan needs a new task.

- [ ] **Step 3: No commit.** Investigation only.

---

### Task F: Migrate `MAX_DAILY_BETS` cap into identifier

**Why:** `run_scan` enforces `if state["today_bets"] >= MAX_DAILY_BETS: skip`. The identifier doesn't, which means once `run_scan` is retired, the cap disappears. The user has historically run with `MAX_DAILY_BETS=10`; preserve.

**Files:**
- Modify: `tennis_dry_run/tennis_identifier.py:main` — count today's selections + placed picks, stop emitting new ones when cap reached.
- Test: `tennis_dry_run/tests/test_tennis_identifier_daily_cap.py` (new file).

- [ ] **Step 1: Write the failing test**

```python
"""Identifier respects the daily-bets cap once run_scan is retired."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


def test_identifier_caps_at_max_daily_bets(tmp_path, monkeypatch):
    """When state.today_bets == MAX_DAILY_BETS, identifier emits zero new selections."""
    import tennis_identifier as ti
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "MAX_DAILY_BETS", 3, raising=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "open_picks": {},
        "today_bets": 3,
        "today_date": "2026-05-14",
    }), encoding="utf-8")
    (state_dir / "trades.jsonl").write_text("", encoding="utf-8")

    # Stub the market+predictor pipeline to make 5 markets qualify in
    # the absence of the cap. Replace fetch + scrape + predict with happy
    # paths.
    # ... (implementation-specific stubs)

    result = ti.main(state_dir=state_dir, vault_dir=tmp_path / "vault",
                     now_utc=datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc))

    assert result["counts"]["qualified"] == 0, \
        f"identifier qualified {result['counts']['qualified']} when daily cap was hit"


def test_identifier_caps_at_remaining_room(tmp_path, monkeypatch):
    """today_bets=8, cap=10 → identifier emits up to 2 qualifying picks max."""
    # ... mirror the above but with cap room of 2
```

- [ ] **Step 2: Run, confirm failure**

```
python -m pytest tests/test_tennis_identifier_daily_cap.py -v
```

- [ ] **Step 3: Implement the cap**

In `tennis_identifier.py:main`, after computing `selections` and before writing pending_selections.jsonl:

```python
    cap = int(os.getenv("TENNIS_MAX_DAILY_BETS",
                        getattr(tdr, "MAX_DAILY_BETS", 10)))
    room = max(0, cap - int(state.get("today_bets", 0)))
    if len(selections) > room:
        log.info("identifier: trimming %d -> %d selections due to daily cap",
                 len(selections), room)
        selections = selections[:room]
    counts["qualified"] = len(selections)
```

**Important:** `today_bets` is the bot's existing counter; identifier should READ it (via state.json) but NOT mutate it. The placer will continue to increment it on each successful place (verify in placer code).

- [ ] **Step 4: Run, confirm pass**

```
python -m pytest tests/test_tennis_identifier_daily_cap.py tests/test_tennis_identifier.py -v
```

- [ ] **Step 5: Verify placer increments today_bets**

Read `tennis_placer.py:place_pick`. Confirm after a successful place, `state["today_bets"] += 1` is part of the atomic mutator. If not, fix it.

- [ ] **Step 6: Commit**

```
git add tennis_dry_run/tennis_identifier.py tennis_dry_run/tests/test_tennis_identifier_daily_cap.py [tennis_placer.py if modified]
git commit -m "feat(tennis_identifier): enforce MAX_DAILY_BETS cap (migrated from run_scan)"
```

---

### Task G: Add `source` field to placer journal rows

**Why:** Task 12 spec finding #11: trade journal entries have no `source` field, so post-hoc audit can't attribute a pick to its originating pipeline. After `run_scan` retirement, EVERY new open row will come from the placer, so the field is technically redundant — but it's cheap insurance for migration cutover and for any future second pipeline.

**Files:**
- Modify: `tennis_dry_run/tennis_placer.py` — write `source: "identifier_placer"` in the journal open row.
- Modify: `tennis_dry_run/tennis_dry_run.py:run_scan` — write `source: "run_scan_legacy"` in the journal open row (so the retirement boundary is grep-able in the historical journal). This line is deleted along with run_scan in the eventual cleanup.
- Test: `tennis_dry_run/tests/test_placer_source_field.py` (new file).

- [ ] **Step 1: Write the failing test**

```python
"""Placer marks journal open rows with source=identifier_placer."""

from __future__ import annotations

import json
from pathlib import Path


def test_placer_writes_source_identifier_placer(tmp_path, monkeypatch):
    """After place_pick completes successfully, the appended journal open row
    must include source='identifier_placer'."""
    # Stub the executor + state file; call place_pick on a happy-path
    # selection; read trades.jsonl; assert the latest open row has
    # source='identifier_placer'.
    # (implementation: copy from existing test_tennis_placer.py happy-path)
    ...
```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Add the field**

In `tennis_placer.py`, find where `result.trade_entry` is constructed (or where the open row is appended to `JOURNAL_FILE`). Add `"source": "identifier_placer"` to the dict.

In `tennis_dry_run.py:run_scan`, find the equivalent journal-append and add `"source": "run_scan_legacy"`. (Removed when run_scan goes.)

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Update `_apply_settled_corrections` to preserve the `source` field**

The helper at `tennis_dry_run.py:_apply_settled_corrections` copies the original `settled` row and overrides specific fields. `source` is on `open` rows only, so the helper doesn't need a change — but verify.

- [ ] **Step 6: Commit**

```
git add tennis_dry_run/tennis_placer.py tennis_dry_run/tennis_dry_run.py tennis_dry_run/tests/test_placer_source_field.py
git commit -m "feat(tennis): tag journal open rows with source (identifier_placer / run_scan_legacy)"
```

---

### Task H: Remove `run_scan` from the bot service main loop

**Why:** The user chose Option 1 of the Task 12 spec: retire `run_scan`. With Tasks F (daily cap) and G (source field) preserving the only non-shared behaviours, the bot's `main()` can drop scan entirely and become a settle-only loop. This eliminates the dual-pipeline race AND the 11 enumerated diffs in one shot.

**Files:**
- Modify: `tennis_dry_run/tennis_dry_run.py:main` — remove the scan branch; keep settle + save.
- Modify: `tennis_dry_run/tennis_dry_run.py:run_scan` — annotate as deprecated; turn body into a no-op that logs a warning and returns the state unchanged. Function preserved for one release cycle so external callers (if any) fail loudly rather than silently.
- Test: `tennis_dry_run/tests/test_settle_only_main.py` (new file).
- Delete: `tennis_dry_run/tests/test_run_scan_excludes_challengers.py` (Task C's interim test is now superfluous — run_scan no-ops).
- Delete: any existing tests of `run_scan`'s happy path that assert it opens picks (they'll fail; replace with a `test_run_scan_is_deprecated_no_op` if useful).

- [ ] **Step 1: Audit existing run_scan tests**

```
grep -rn "run_scan" tennis_dry_run/tests/ tennis_dry_run/*.py | grep -v "_apply_settled_corrections"
```

Categorise each:
- Tests that assert behaviour of `run_scan` opening picks → DELETE.
- Tests that assert behaviour of `run_scan` skipping (e.g., Challenger exclusion from Task C) → DELETE (subsumed by no-op).
- Production callers of `run_scan` other than `main()` → there shouldn't be any; if there are, escalate.

- [ ] **Step 2: Write the new main-loop test**

Create `tennis_dry_run/tests/test_settle_only_main.py`:

```python
"""After Task H, bot main loop must settle-and-save without scanning."""

from __future__ import annotations

import json
from unittest.mock import patch


def test_main_loop_does_not_call_run_scan(monkeypatch, tmp_path):
    """The continuous loop in main() must not invoke run_scan in the new
    architecture. Identifier+placer (cron+at) own picking."""
    import tennis_dry_run as tdr

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")
    (tmp_path / "state.json").write_text(json.dumps({
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {}, "today_bets": 0, "today_date": "2026-05-14",
    }), encoding="utf-8")

    scan_count = {"n": 0}
    def _fake_run_scan(state):
        scan_count["n"] += 1
        return state

    monkeypatch.setattr(tdr, "run_scan", _fake_run_scan)
    monkeypatch.setattr(tdr, "scrape_completed_results", lambda *a, **kw: [])
    monkeypatch.setattr(tdr, "should_stop", lambda iteration: iteration >= 2)

    tdr.main()  # 2 iterations, then exits via should_stop

    assert scan_count["n"] == 0, \
        f"run_scan was called {scan_count['n']} times in the retired loop"


def test_run_scan_is_deprecated_no_op(monkeypatch, tmp_path, caplog):
    import tennis_dry_run as tdr
    state = {"open_picks": {}, "today_bets": 0}
    result = tdr.run_scan(state)
    assert result == state, "run_scan must return state unchanged"
    assert any("deprecated" in r.message.lower() for r in caplog.records), \
        "run_scan must log a deprecation warning"
```

**Note:** the test introduces a `should_stop(iteration)` predicate that doesn't exist in main() yet. You'll need to add a small loop-control seam to main() to make it testable. Acceptable shapes:
- Add `iteration` counter inside the loop and call `if should_stop(iteration): break` (default `should_stop = lambda i: False`).
- OR raise `KeyboardInterrupt` after a fixture-controlled number of iterations.

Use whichever is least invasive.

- [ ] **Step 3: Run, confirm failure**

```
python -m pytest tests/test_settle_only_main.py -v
```
Expected: FAIL because `run_scan` IS currently called from `main()`.

- [ ] **Step 4: Modify `main()` — remove the scan branch**

In `tennis_dry_run.py:main`, find the section that calls `run_scan` (likely inside a `if now.hour in SCAN_TIMES_UTC:` branch or similar). Delete the scan branch entirely. Keep:
- The settle branch (`if elapsed_since_settle > SETTLE_INTERVAL: state, settled_ids = run_settle(...); save_state(...)`).
- The save-on-exit / KeyboardInterrupt handler.

Add at the top of `main()`:
```python
log.info("Tennis bot starting in SETTLE-ONLY mode (run_scan retired 2026-05-13; "
         "picks come from cron identifier + at-spawned placer)")
```

Update the existing startup log line to drop the mention of `scan_times_utc`.

- [ ] **Step 5: Convert `run_scan` to a deprecated no-op**

```python
def run_scan(state: dict) -> dict:
    """DEPRECATED 2026-05-13. Pick creation moved to tennis_identifier +
    tennis_placer (cron + at). This function is preserved as a no-op for
    one release cycle to surface any external callers; remove in the
    cycle after.
    """
    log.warning("run_scan called — DEPRECATED; identifier+placer owns pick "
                "creation now. Returning state unchanged.")
    return state
```

Move every helper used only by the old `run_scan` body (e.g., `fetch_book`, predictor wrapper) — wait, those are likely still used by identifier+placer. Don't move; leave in place.

- [ ] **Step 6: Delete the Task C interim test**

```
git rm tennis_dry_run/tests/test_run_scan_excludes_challengers.py
```

The behaviour is now vacuously true (run_scan is a no-op).

- [ ] **Step 7: Delete any other run_scan happy-path tests**

Per Step 1 audit. Replace with tests that assert the no-op behaviour if you want coverage.

- [ ] **Step 8: Run focused tests**

```
python -m pytest tests/test_settle_only_main.py tests/test_tennis_dry_run_settle.py -v
```

- [ ] **Step 9: Full suite**

```
python -m pytest tests/ -q
```
Expected: at least 187 passed (some Task C tests removed, settle-only tests added).

- [ ] **Step 10: Commit**

```
git add tennis_dry_run/tennis_dry_run.py tennis_dry_run/tests/test_settle_only_main.py
git rm tennis_dry_run/tests/test_run_scan_excludes_challengers.py
git commit -m "feat(tennis): retire run_scan; bot becomes settle-only (identifier+placer own picking; Task 12 Option 1)"
```

---

### Task I: Deploy and verify end-to-end

**Why:** Final ship. Bot service restarted with the new main loop; identifier runs at next cron tick (07:00 / 14:00 UTC); placer runs at T-15min before each scheduled match.

**Files:**
- No code change. Operational only.

- [ ] **Step 1: Pre-deploy verification**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -m pytest tests/ -q
```
Expected: all green except the Windows-only skipped module.

- [ ] **Step 2: Back up VPS state**

```
ssh vps "TS=\$(date +%s) && cp /opt/tennis-dry-run/.tmp/trades.jsonl /opt/tennis-dry-run/.tmp/trades.jsonl.bak.\$TS && cp /opt/tennis-dry-run/.tmp/state.json /opt/tennis-dry-run/.tmp/state.json.bak.\$TS && ls -la /opt/tennis-dry-run/.tmp/*.bak.\$TS"
```

- [ ] **Step 3: Sync code**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
scp tennis_dry_run.py tennis_identifier.py tennis_placer.py tennis_kelly.py vps:/opt/tennis-dry-run/
```

- [ ] **Step 4: Smoke-test imports**

```
ssh vps "cd /opt/tennis-dry-run && venv/bin/python -c 'import tennis_dry_run, tennis_identifier, tennis_placer, tennis_kelly; from tennis_dry_run import run_scan; print(run_scan({\"open_picks\": {}}))'"
```
Expected: prints `{'open_picks': {}}` and a deprecation warning log line.

- [ ] **Step 5: Restart bot**

```
ssh vps "systemctl restart tennis-dry-run.service && sleep 4 && systemctl status tennis-dry-run.service --no-pager | head -8"
```

Check log: should see `SETTLE-ONLY mode` startup line, no mention of `scan_times_utc`.

- [ ] **Step 6: Trigger identifier manually (optional, for instant verification)**

If you want to verify immediately rather than wait for the next cron tick:

```
ssh vps "cd /opt/tennis-dry-run && venv/bin/python -c 'import tennis_identifier; tennis_identifier.main()' 2>&1 | tail -20"
```

The identifier should scan, qualify selections within the daily cap, write pending_selections.jsonl, and `at`-spawn placers per selection.

- [ ] **Step 7: Verify next BOD report (or wait for 07:00 UTC cron)**

```
ssh vps "ls -la /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports/ | tail -3"
```

Inspect the newest report for:
- Scan Summary shows Qualified count (identifier is now the only source).
- "Bot-opened (not counted in Qualified)" annotation should be `0` (or not appear) since run_scan no longer opens.
- Journal `source: "identifier_placer"` on every new open row.

```
ssh vps "tail -20 /opt/tennis-dry-run/.tmp/trades.jsonl | python3 -m json.tool 2>/dev/null || ssh vps 'tail -5 /opt/tennis-dry-run/.tmp/trades.jsonl'"
```

- [ ] **Step 8: No commit.** Operational only.

---

## Phase 4 — Ship

### Task J: Push commits to GitHub

**Why:** 17+ commits ahead of `origin/main`. Final push.

- [ ] **Step 1: Confirm clean tree + main branch**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas"
git status
git branch --show-current
```

- [ ] **Step 2: Sanity check no secrets crept in**

```
git diff origin/main..HEAD -- '*.env*' '*.json' 'credentials*' 2>&1 | head
```

Expected: empty (no env/secret files in the diff).

- [ ] **Step 3: Push**

```
git push origin main
```

- [ ] **Step 4: Confirm**

```
git log --oneline @{u}..HEAD
```
Expected: empty (HEAD == upstream).

---

## Phase 5 — Verification and post-deploy

### Task K: End-to-end audit cross-check

**Why:** Mirror Task 13 of the parent plan — verify the live VPS state still reconciles after the retirement deploy.

- [ ] **Step 1: Run the audit script**

```bash
ssh vps "cd /opt/tennis-dry-run && venv/bin/python << 'PY'
import json, sys
sys.path.insert(0, '/opt/tennis-dry-run')
from datetime import date
from tennis_kelly import replay_three_bankrolls
from tennis_dry_run import _apply_settled_corrections

with open('/opt/tennis-dry-run/.tmp/state.json') as f:
    state = json.load(f)
rows = [json.loads(ln) for ln in open('/opt/tennis-dry-run/.tmp/trades.jsonl') if ln.strip()]
effective = _apply_settled_corrections(rows)
placed = [t for t in effective if t.get('type')=='open']
settled = [t for t in effective if t.get('type')=='settled']

sum_pnl = sum(s['pnl'] for s in settled)
print(f'expected balance: {500.0 + sum_pnl:.4f}  state.balance: {state[\"balance\"]:.4f}')
assert abs((500.0 + sum_pnl) - state['balance']) < 0.01, 'STATE / JOURNAL MISMATCH'

r = replay_three_bankrolls(settled, placed, starting_balance=500.0, today=date.today())
expected_deployed = 25.0 * len(state.get('open_picks', {}))
print(f'expected base deployed: {expected_deployed:.2f}  replay base deployed: {r[\"base\"][\"deployed\"]:.2f}')
assert abs(r['base']['deployed'] - expected_deployed) < 1.0, 'PHANTOM DEPLOYED'

source_counts = {}
for o in [r for r in rows if r.get('type')=='open']:
    source_counts[o.get('source', '<missing>')] = source_counts.get(o.get('source', '<missing>'), 0) + 1
print('source attribution:', source_counts)

print('PASS')
PY"
```

Expected: `PASS`. After retirement, all NEW open rows have `source: "identifier_placer"`; legacy rows (pre-Task G) will show `<missing>`.

- [ ] **Step 2: Update memory note**

Refresh `tennis_trading/project_reporting_accuracy_remediation.md` to add a final line: "Pipeline retirement (Task 12 Option 1) shipped 2026-MM-DD. Bot service is settle-only; identifier+placer owns picking."

---

## Rollback plan

If anything breaks production after Task H+I deploy:

```bash
# 1. Restore the previous main loop (run_scan-enabled) by checking out the
#    pre-retirement commit on the VPS:
ssh vps "ls -t /opt/tennis-dry-run/tennis_dry_run.py.bak.* | head -1"
# (manually cp the most recent .bak. back into place, OR scp the
#  pre-retirement file from local git)

# 2. Restart:
ssh vps "systemctl restart tennis-dry-run.service"

# 3. Verify the bot is opening picks again:
ssh vps "journalctl -u tennis-dry-run.service --since '2 minutes ago' --no-pager | tail -20"
```

For state-level corruption (unlikely, since the retirement only changes which process WRITES picks, not the journal schema):

```bash
# Restore state + journal from the Task I Step 2 backups:
ssh vps "ls -t /opt/tennis-dry-run/.tmp/state.json.bak.* | head -1 | xargs -I {} cp {} /opt/tennis-dry-run/.tmp/state.json"
ssh vps "ls -t /opt/tennis-dry-run/.tmp/trades.jsonl.bak.* | head -1 | xargs -I {} cp {} /opt/tennis-dry-run/.tmp/trades.jsonl"
ssh vps "systemctl restart tennis-dry-run.service"
```

---

## Out of scope for this plan (track separately)

- Remove `run_scan` function entirely (currently a deprecated no-op). Do in the cycle after this plan ships, once any external callers have surfaced.
- Settlement-only loop refactor: the bot service still has the legacy "continuous scan + settle" structure. After Task H it's "continuous settle". Whether to move settlement to a cron tick (e.g., every 30 minutes) instead of a continuous loop is a separate operational decision.
- Migrate any `MAX_DAILY_BETS` config to the identifier's env vars cleanly (today it's read from the bot's module — Task F crosses module boundaries, which is fine for now but worth tidying).
- Audit `tennis_kelly.py` for any OTHER unrounded balance/pnl accumulation paths beyond the one Task B covers.
- Push button on the "remove run_scan entirely" cleanup once one full release cycle has elapsed with no callers logging the deprecation warning.

---

## Engineer checklist before declaring "done"

- [ ] Task A: c692c62 reviewed; any issues logged or fixed.
- [ ] Task B: Kelly replay balance rounds; test green.
- [ ] Task C: Interim Challenger exclusion in run_scan; test green (will be deleted at Task H — that's expected).
- [ ] Task D: Linux flock concurrency test PASSES on VPS.
- [ ] Task E: Audit complete; no unmigrated run_scan behaviour found.
- [ ] Task F: Daily cap enforced in identifier; placer increments today_bets verified.
- [ ] Task G: New open rows carry `source` field.
- [ ] Task H: `run_scan` is a no-op; main loop is settle-only; tests rewritten.
- [ ] Task I: Deployed; bot logs "SETTLE-ONLY mode"; next BOD shows zero Bot-opened picks.
- [ ] Task J: Pushed to origin/main.
- [ ] Task K: Audit cross-check PASS; memory note updated.
- [ ] All tests green (`pytest tests/` in `tennis_dry_run/`).
- [ ] One full day's BOD+EOD report has rendered correctly post-retirement.

---

## Plan-level notes for the executing agent

- This plan assumes the 2026-05-13 reporting-accuracy plan has been completed and deployed. If `c692c62` is not the current HEAD or `_apply_settled_corrections` is not in `tennis_dry_run.py`, **stop and resync** — that's a different starting state.
- The retirement (Task H) is the riskiest change. Do Tasks A–G first to land the polish + migrations under a green test suite; only then take Task H.
- Don't push to GitHub (Task J) until Task I has been observed running cleanly for at least one BOD cycle. If something breaks at 07:00 UTC the next day, you want main to still be the pre-push HEAD for `git diff` purposes.
- All ssh / production operations are gated by the user. If a step says "ssh vps", that's an authorised production op — but if you're running this plan in a fresh session and the user hasn't reaffirmed the authorisation, ASK before each ssh block.
