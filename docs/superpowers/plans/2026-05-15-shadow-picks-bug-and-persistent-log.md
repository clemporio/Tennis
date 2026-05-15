# Shadow Picks Bug Fix + Persistent Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 2026-05-11 "zero-resolved shadow picks" bug (root cause: identifier prunes today's shadow selections too aggressively), then add a persistent `shadow_outcomes.jsonl` log + backfill + cumulative EOD block so tier-B calibration becomes analysable from a single rolling source.

**Architecture:** All changes are local to existing modules in `tennis_dry_run/`. The prune fix splits shadow pruning off `prune_stale_pending` into a date-based `prune_shadow_stale` (keep all of today's UTC date) — pending-track behaviour is unchanged. The persistent log is an append-only JSONL with `(pick_id, status_resolved_at)` dedup; backfill ingests existing markdown daily-report tables. The cumulative renderer is additive to `tennis_portfolio.py` and called once from `write_eod_report` after `render_shadow_picks_block`.

**Tech Stack:** Python 3.13, pytest 9, regex (markdown parsing for backfill). No new runtime deps.

**Context — research note this plan implements:** `vault/finance-brain/10-Projects/Tennis-Automated/shadow-picks-analysis-2026-05-15.md`. Aggregate (n=8, 50% hit vs ~74% predicted) extracted manually from per-day markdown because there is no rolling log. The 2026-05-11 zero-resolved anomaly was hypothesised to be in `tennis_eod_report.py:174-188` (`game_time` filter); the actual root cause identified by codebase inspection is `tennis_identifier.prune_stale_pending` running before EOD and removing today's shadow rows whose `game_time + 60min grace` has passed.

**Root cause (verified by reading the prune fn + identifier main loop):**
- `prune_stale_pending` (tennis_identifier.py:254-306) is shared between `pending_selections.jsonl` and `shadow_selections.jsonl`. It drops entries where `game_time < now - 60min`.
- Identifier runs multiple times per day on cron. By 22:00 UTC EOD, any tier-B pick whose match was earlier in the day has been pruned. E.g. 2026-05-11 Noskova @ 09:00 UTC, Nakashima @ 10:10 UTC — both pruned by any identifier run after ~11:10 UTC.
- For `pending_selections.jsonl` the prune is correct (placer fires once at T-15, downstream done after game_time). For `shadow_selections.jsonl` EOD at 22:00 UTC is a legitimate downstream reader.

---

## File structure

**Create:**
- `tennis_dry_run/tools/backfill_shadow_outcomes.py` — one-shot script that ingests existing `Daily-Reports/YYYY-MM-DD.md` shadow tables into `shadow_outcomes.jsonl`.
- `tennis_dry_run/tests/test_backfill_shadow_outcomes.py` — unit tests for the backfill parser + writer.
- `tennis_dry_run/tests/test_shadow_outcomes_log.py` — tests for the EOD JSONL-writer hook + idempotency.

**Modify:**
- `tennis_dry_run/tennis_identifier.py` — add `prune_shadow_stale`, swap the shadow-side call in `main()`.
- `tennis_dry_run/tests/test_tennis_identifier.py` — add tests for `prune_shadow_stale`.
- `tennis_dry_run/tennis_eod_report.py` — append resolved outcomes to `STATE_DIR/shadow_outcomes.jsonl`; call new cumulative renderer.
- `tennis_dry_run/tennis_portfolio.py` — add `render_shadow_cumulative_block`.
- `tennis_dry_run/tests/test_tennis_eod_report.py` — add tests for the JSONL writer + cumulative block wiring.
- `tennis_dry_run/tests/test_tennis_portfolio.py` — add tests for the cumulative renderer.

**No changes to:** `tennis_shadow_placer.py`, `tennis_dry_run.py`, `tennis_kelly.py`, `tennis_executor.py`, `tennis_signing.py`, `tennis_sxbet.py`.

---

## Phase 1 — Fix the 2026-05-11 zero-resolved bug

### Task 1: Failing test that demonstrates the bug

**Files:**
- Modify: `tennis_dry_run/tests/test_tennis_identifier.py` — add one test in the `prune_stale_pending` section (~line 466).

This test pins the current (buggy) behaviour as it applies to shadow selections: with grace=60min, a 09:00 UTC shadow pick is dropped from `shadow_selections.jsonl` when identifier prunes at 11:00 UTC. We'll mark it `pytest.mark.xfail(strict=True)` for the duration of Task 2; Task 3 flips it into a positive assertion against the new function.

- [ ] **Step 1: Add the reproduction test.**

```python
def test_prune_stale_pending_eats_today_shadow_picks_REGRESSION(tmp_path):
    """REGRESSION: 2026-05-11 bug — identifier's prune (60-min grace) removes
    today's shadow picks whose match was earlier in the day, so 22:00 UTC EOD
    sees an empty file and reports 'No shadow picks today' even though tier-B
    picks fired through the identifier. Documents the broken behaviour; Task 2
    introduces prune_shadow_stale (date-based) as the fix.
    """
    shadow = tmp_path / "shadow_selections.jsonl"
    now = datetime(2026, 5, 11, 11, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xnoskova", "pick": "Linda Noskova", "opponent": "Sara Errani",
         "league": "WTA Rome", "tier": "B",
         "game_time": _ts(2026, 5, 11, 9, 0)},  # today 09:00 UTC, 2h ago
        {"pick_id": "0xnakashima", "pick": "Brandon Nakashima", "opponent": "Alex De Minaur",
         "league": "ATP Rome", "tier": "B",
         "game_time": _ts(2026, 5, 11, 10, 10)},  # today 10:10 UTC, 50min ago
    ]
    _write_pending(shadow, rows)

    ti.prune_stale_pending(shadow, now_utc=now, grace_minutes=60)

    import json
    surviving = [json.loads(l) for l in shadow.read_text(encoding="utf-8").splitlines() if l.strip()]
    # The bug: today's shadow picks are gone. Noskova (>60min ago) pruned;
    # Nakashima (50min ago, within grace) kept. EOD at 22:00 UTC after a
    # second identifier run would see neither.
    surviving_ids = {r["pick_id"] for r in surviving}
    assert "0xnoskova" not in surviving_ids  # pruned — that IS the bug
    assert "0xnakashima" in surviving_ids    # still in grace at 11:00 UTC
```

- [ ] **Step 2: Run it.**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -m pytest tests/test_tennis_identifier.py::test_prune_stale_pending_eats_today_shadow_picks_REGRESSION -v
```
Expected: PASS (this test pins current buggy behaviour as documentation; it's NOT xfail).

- [ ] **Step 3: Commit.**

```
git add tennis_dry_run/tests/test_tennis_identifier.py
git commit -m "test(tennis): pin 2026-05-11 shadow prune regression"
```

---

### Task 2: New `prune_shadow_stale` function (date-based)

**Files:**
- Modify: `tennis_dry_run/tennis_identifier.py` — add new function below `prune_stale_pending` (~line 307).
- Modify: `tennis_dry_run/tests/test_tennis_identifier.py` — new tests for the new function.

**Rationale for a separate function (not a `mode=` flag):** The semantics differ. `prune_stale_pending` is "post-game grace" (placer is the consumer, done shortly after game_time). `prune_shadow_stale` is "today only" (EOD at 22:00 UTC is the consumer). Forcing both into one signature obscures the contract. Two functions, each with one clear job.

- [ ] **Step 1: Write the failing tests.**

Add to `tests/test_tennis_identifier.py` (below the existing prune tests, ~line 590):

```python
# ── prune_shadow_stale ────────────────────────────────────────────────────────

def test_prune_shadow_stale_keeps_all_of_today_regardless_of_game_time(tmp_path):
    """All entries whose game_time falls on today's UTC date are kept, even if
    the match itself was hours ago. EOD at 22:00 UTC needs every tier-B pick
    fired earlier in the day."""
    shadow = tmp_path / "shadow_selections.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xa", "pick": "Linda Noskova",
         "game_time": _ts(2026, 5, 11, 9, 0)},   # today, 13h ago → KEEP
        {"pick_id": "0xb", "pick": "Brandon Nakashima",
         "game_time": _ts(2026, 5, 11, 10, 10)}, # today, 12h ago → KEEP
        {"pick_id": "0xc", "pick": "Future Pick",
         "game_time": _ts(2026, 5, 11, 23, 30)}, # today, future → KEEP
        {"pick_id": "0xd", "pick": "Yesterday",
         "game_time": _ts(2026, 5, 10, 14, 0)},  # yesterday → PRUNE
    ]
    _write_pending(shadow, rows)

    result = ti.prune_shadow_stale(shadow, now_utc=now)

    assert result["pruned"] == 1
    assert result["kept"] == 3
    assert result["pruned_picks"] == ["Yesterday"]

    import json
    surviving = [json.loads(l) for l in shadow.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert {r["pick_id"] for r in surviving} == {"0xa", "0xb", "0xc"}


def test_prune_shadow_stale_handles_missing_file(tmp_path):
    """Missing file → no-op (mirrors prune_stale_pending)."""
    shadow = tmp_path / "does_not_exist.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)

    result = ti.prune_shadow_stale(shadow, now_utc=now)

    assert result == {"kept": 0, "pruned": 0, "pruned_picks": []}
    assert not shadow.exists()


def test_prune_shadow_stale_keeps_entries_without_game_time(tmp_path):
    """Defensive: unknown timing → don't drop."""
    shadow = tmp_path / "shadow.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    rows = [{"pick_id": "0xnogt", "pick": "Mystery"}]
    _write_pending(shadow, rows)

    result = ti.prune_shadow_stale(shadow, now_utc=now)

    assert result["kept"] == 1
    assert result["pruned"] == 0


def test_prune_shadow_stale_skips_malformed_lines(tmp_path):
    """Malformed JSON dropped silently, valid rows preserved."""
    shadow = tmp_path / "shadow.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    shadow.write_text(
        '{"pick_id": "0xtoday", "pick": "Today", "game_time": ' + str(_ts(2026, 5, 11, 9)) + '}\n'
        'not-json\n'
        '\n'
        '{"pick_id": "0xyest", "pick": "Yesterday", "game_time": ' + str(_ts(2026, 5, 10, 9)) + '}\n',
        encoding="utf-8",
    )

    result = ti.prune_shadow_stale(shadow, now_utc=now)

    assert result["pruned"] == 1
    assert result["kept"] == 1


def test_prune_shadow_stale_is_atomic(tmp_path, monkeypatch):
    """If os.replace fails, the original file is preserved."""
    shadow = tmp_path / "shadow.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    rows = [
        {"pick_id": "0xtoday", "pick": "Today", "game_time": _ts(2026, 5, 11, 9)},
        {"pick_id": "0xyest", "pick": "Yesterday", "game_time": _ts(2026, 5, 10, 9)},
    ]
    _write_pending(shadow, rows)
    original_bytes = shadow.read_bytes()

    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("tennis_identifier.os.replace", boom)

    with pytest.raises(OSError):
        ti.prune_shadow_stale(shadow, now_utc=now)
    assert shadow.read_bytes() == original_bytes
```

- [ ] **Step 2: Run them to confirm they fail.**

```
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -m pytest tests/test_tennis_identifier.py -k prune_shadow_stale -v
```
Expected: 5 FAILs with `AttributeError: module 'tennis_identifier' has no attribute 'prune_shadow_stale'`.

- [ ] **Step 3: Implement `prune_shadow_stale`.**

Insert into `tennis_dry_run/tennis_identifier.py` immediately after `prune_stale_pending` (after line 306):

```python
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

    Returns: {"kept": int, "pruned": int, "pruned_picks": list[str]}.
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
```

- [ ] **Step 4: Run the tests to confirm they pass.**

```
python -m pytest tests/test_tennis_identifier.py -k prune_shadow_stale -v
```
Expected: 5 PASSED.

- [ ] **Step 5: Commit.**

```
git add tennis_dry_run/tennis_identifier.py tennis_dry_run/tests/test_tennis_identifier.py
git commit -m "feat(tennis): add prune_shadow_stale (date-based prune for shadow selections)"
```

---

### Task 3: Wire `prune_shadow_stale` into identifier main()

**Files:**
- Modify: `tennis_dry_run/tennis_identifier.py:517-527` — swap the shadow-side prune call.

- [ ] **Step 1: Replace the shared-prune loop with two separate calls.**

Locate lines 517-527 in `tennis_dry_run/tennis_identifier.py`:

```python
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
```

Replace with:

```python
    prune_grace_min = int(os.getenv("PENDING_PRUNE_GRACE_MIN", "60"))
    # shadow_placements.jsonl is an append-only audit log (keyed by ts, not
    # game_time) — don't prune it. It grows ~one line per tier-B pick per day.
    try:
        r = prune_stale_pending(pending_file, now_utc, grace_minutes=prune_grace_min)
        if r["pruned"]:
            log.info(
                "Pruned %d stale pending selection(s) (kept=%d): %s",
                r["pruned"], r["kept"], ", ".join(r["pruned_picks"]),
            )
    except Exception as exc:
        log.warning("pending prune failed: %s", exc)

    # Shadow selections use a date-based prune (keep today's UTC date) because
    # the EOD report at 22:00 UTC needs every tier-B pick from today regardless
    # of whether the match itself is over. Grace-based pruning would silently
    # eat today's completed shadow picks (2026-05-11 bug).
    try:
        r = prune_shadow_stale(shadow_file, now_utc)
        if r["pruned"]:
            log.info(
                "Pruned %d stale shadow selection(s) (kept=%d): %s",
                r["pruned"], r["kept"], ", ".join(r["pruned_picks"]),
            )
    except Exception as exc:
        log.warning("shadow prune failed: %s", exc)
```

- [ ] **Step 2: Add an integration test asserting identifier preserves today's shadow picks past game_time.**

Add to `tests/test_tennis_identifier.py` at the end of the file:

```python
def test_identifier_main_preserves_today_shadow_picks_past_game_time(tmp_path, monkeypatch):
    """Regression: 2026-05-11 bug. Identifier running at 22:00 UTC must NOT
    prune today's shadow picks whose game_time was earlier today. The new
    prune_shadow_stale (date-based) replaces the shared grace-based prune for
    the shadow file.
    """
    import tennis_identifier as ti_mod

    # Seed shadow_selections.jsonl with an early-day pick.
    shadow_file = tmp_path / "shadow_selections.jsonl"
    pending_file = tmp_path / "pending_selections.jsonl"
    now = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    rows = [{
        "pick_id": "0xnoskova", "pick": "Linda Noskova", "opponent": "Sara Errani",
        "league": "WTA Rome", "tier": "B",
        "game_time": _ts(2026, 5, 11, 9, 0),  # 13h before now → grace-prune
                                              # would eat this, date-prune keeps
    }]
    _write_pending(shadow_file, rows)

    # Exercise prune-only path: monkeypatch the SX market fetch to return
    # nothing so identifier exits cleanly after the prune step.
    class _FakeSX:
        def __init__(self, *a, **kw): pass
        def get_all_tennis_markets(self): return []
    monkeypatch.setattr("tennis_sxbet.TennisSXBet", _FakeSX)
    monkeypatch.setattr(ti_mod, "STATE_DIR", tmp_path)
    monkeypatch.setenv("PENDING_SELECTIONS_FILE", str(pending_file))
    monkeypatch.setenv("SHADOW_SELECTIONS_FILE", str(shadow_file))
    monkeypatch.setenv("OBSIDIAN_VAULT_DIR", "")
    # Freeze "now" — identifier reads datetime.now(timezone.utc) internally.
    import datetime as _dt
    class _FrozenDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(ti_mod, "datetime", _FrozenDT)

    rc = ti_mod.main()
    assert rc == 0

    import json
    surviving = [json.loads(l) for l in shadow_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r["pick_id"] == "0xnoskova" for r in surviving), \
        "today's shadow pick must survive identifier's prune"
```

- [ ] **Step 3: Run the test (it must pass).**

```
python -m pytest tests/test_tennis_identifier.py::test_identifier_main_preserves_today_shadow_picks_past_game_time -v
```
Expected: PASS.

- [ ] **Step 4: Run the full identifier test module to verify no regressions.**

```
python -m pytest tests/test_tennis_identifier.py -v
```
Expected: all existing tests pass + 6 new ones (1 regression + 5 new fn + 1 integration).

- [ ] **Step 5: Commit.**

```
git add tennis_dry_run/tennis_identifier.py tennis_dry_run/tests/test_tennis_identifier.py
git commit -m "fix(tennis): use date-based prune for shadow_selections (2026-05-11 bug)"
```

---

## Phase 2 — Persistent `shadow_outcomes.jsonl` log

### Task 4: Append resolved outcomes from `write_eod_report` with idempotent dedup

**Files:**
- Create: `tennis_dry_run/tests/test_shadow_outcomes_log.py`
- Modify: `tennis_dry_run/tennis_eod_report.py:210-217` — append the JSONL after `resolve_shadow_outcomes`.

**Schema for `shadow_outcomes.jsonl` (one line per resolved outcome):**

```json
{
  "pick_id": "0xabc...",
  "pick": "Ben Shelton",
  "opponent": "Nikoloz Basilashvili",
  "league": "ATP Rome",
  "surface": "clay",
  "model_prob": 0.7753,
  "fair_odds": 1.290,
  "tier": "B",
  "game_time": 1778336400,
  "game_time_iso": "2026-05-09T14:20:00+00:00",
  "status": "WIN",
  "theoretical_pnl": 7.25,
  "result_winner": "Shelton B.",
  "resolved_at": "2026-05-09T22:00:00+00:00"
}
```

Dedup key: `(pick_id, status)` — re-running EOD on the same day with `status` unchanged is a no-op. If `status` flips from `"pending"` to `"WIN"`/`"LOSS"`/`"RETIRED"` (e.g. results posted late), the newer line is appended and supersedes; readers always take the latest `resolved_at` per `pick_id`.

- [ ] **Step 1: Write failing tests.**

Create `tennis_dry_run/tests/test_shadow_outcomes_log.py`:

```python
"""Tests for the persistent shadow_outcomes.jsonl append-only log written
by tennis_eod_report.write_eod_report after resolve_shadow_outcomes.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _ts_unix(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_eod_appends_resolved_shadow_outcomes_to_jsonl(tmp_path, monkeypatch):
    """After resolve_shadow_outcomes, write_eod_report must append each
    resolved row (status in {WIN, LOSS, RETIRED}) to shadow_outcomes.jsonl
    with a resolved_at timestamp. Pending rows are NOT appended."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    shadow_rows = [
        {"pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Basilashvili",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 14, 0)},
        {"pick_id": "0xsab", "pick": "Aryna Sabalenka", "opponent": "Linette",
         "league": "WTA Rome", "surface": "clay",
         "model_prob": 0.74, "fair_odds": 1.351, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 16, 0)},
        {"pick_id": "0xpending", "pick": "Future Player", "opponent": "X",
         "league": "ATP Rome", "surface": "clay",
         "model_prob": 0.75, "fair_odds": 1.333, "tier": "B",
         "game_time": _ts_unix(2026, 5, 9, 20, 0)},
    ]
    (state_dir / "shadow_selections.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow_rows) + "\n"
    )

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
            {"player_a": "Sabalenka A.", "player_b": "Linette M.",
             "winner": "Linette M.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    log_path = state_dir / "shadow_outcomes.jsonl"
    assert log_path.exists()
    rows = _read_jsonl(log_path)
    # Only resolved (WIN/LOSS/RETIRED) — pending is excluded.
    assert {r["pick_id"] for r in rows} == {"0xshelt", "0xsab"}
    by_id = {r["pick_id"]: r for r in rows}
    assert by_id["0xshelt"]["status"] == "WIN"
    assert by_id["0xshelt"]["theoretical_pnl"] == pytest.approx(7.25)
    assert by_id["0xshelt"]["result_winner"] == "Shelton B."
    assert by_id["0xsab"]["status"] == "LOSS"
    assert by_id["0xsab"]["theoretical_pnl"] == pytest.approx(-25.0)
    assert by_id["0xshelt"]["resolved_at"] == "2026-05-09T22:00:00+00:00"
    # Preserved input fields
    assert by_id["0xshelt"]["model_prob"] == pytest.approx(0.7753)
    assert by_id["0xshelt"]["fair_odds"] == pytest.approx(1.290)
    assert by_id["0xshelt"]["tier"] == "B"


def test_eod_shadow_log_is_idempotent_on_rerun(tmp_path, monkeypatch):
    """Running EOD twice with identical outcomes appends each row only once.
    Dedup key: (pick_id, status)."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")
    (state_dir / "shadow_selections.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Basilashvili",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
        "game_time": _ts_unix(2026, 5, 9, 14, 0),
    }) + "\n")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)
    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    rows = _read_jsonl(state_dir / "shadow_outcomes.jsonl")
    assert len(rows) == 1
    assert rows[0]["pick_id"] == "0xshelt"


def test_eod_shadow_log_appends_when_pending_becomes_resolved(tmp_path, monkeypatch):
    """If a pick was pending yesterday and resolves today, a new row is
    appended for the resolved status. Readers take the latest resolved_at."""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")

    # Pre-seed the log with a hypothetical pending entry for the same pick.
    (state_dir / "shadow_outcomes.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "status": "pending",
        "theoretical_pnl": 0.0, "result_winner": None,
        "resolved_at": "2026-05-08T22:00:00+00:00",
    }) + "\n")

    (state_dir / "shadow_selections.jsonl").write_text(json.dumps({
        "pick_id": "0xshelt", "pick": "Ben Shelton", "opponent": "Basilashvili",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.7753, "fair_odds": 1.290, "tier": "B",
        "game_time": _ts_unix(2026, 5, 9, 14, 0),
    }) + "\n")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [
            {"player_a": "Shelton B.", "player_b": "Basilashvili N.",
             "winner": "Shelton B.", "tournament": "Rome"},
        ],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    rows = _read_jsonl(state_dir / "shadow_outcomes.jsonl")
    # Two rows: pending (seeded) + WIN (newly resolved).
    assert len(rows) == 2
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["WIN", "pending"]


def test_eod_writes_no_log_when_no_shadow_picks(tmp_path, monkeypatch):
    """If there are no shadow selections, the log file is NOT created. (No
    silent empty-file pollution.)"""
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")
    (state_dir / "shadow_selections.jsonl").write_text("")

    monkeypatch.setattr(
        "tennis_dry_run.scrape_completed_results",
        lambda target_date=None: [],
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

    write_eod_report(now_utc=now, state_dir=state_dir, vault_dir=vault_dir)

    log_path = state_dir / "shadow_outcomes.jsonl"
    assert not log_path.exists()
```

- [ ] **Step 2: Run them to confirm they fail.**

```
python -m pytest tests/test_shadow_outcomes_log.py -v
```
Expected: 4 FAILs (log file doesn't exist).

- [ ] **Step 3: Implement the JSONL writer in `tennis_eod_report.py`.**

Add a helper function above `write_eod_report` (after `resolve_shadow_outcomes`, ~line 117):

```python
def append_shadow_outcomes_log(
    enriched: list[dict],
    log_file: Path,
    now_utc: datetime,
) -> int:
    """Append resolved shadow rows to `shadow_outcomes.jsonl` with idempotent
    `(pick_id, status)` dedup. Pending rows are NOT written.

    Returns the number of new rows appended.

    The log is the single source of truth for tier-B calibration analysis.
    Per-day markdown reports are derived; this is authoritative.
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
```

Then modify `write_eod_report` to call it. Replace the existing block at lines 210-217:

```python
    if shadow_today:
        try:
            from tennis_dry_run import scrape_completed_results
            completed = scrape_completed_results(target_date=today)
        except Exception as exc:
            log.warning("Shadow outcome scrape failed: %s", exc)
            completed = []
        shadow_today = resolve_shadow_outcomes(shadow_today, completed)
```

with:

```python
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
```

- [ ] **Step 4: Run the tests to confirm they pass.**

```
python -m pytest tests/test_shadow_outcomes_log.py -v
```
Expected: 4 PASSED.

- [ ] **Step 5: Run full eod-report test module to verify no regressions.**

```
python -m pytest tests/test_tennis_eod_report.py -v
```
Expected: all existing tests still pass.

- [ ] **Step 6: Commit.**

```
git add tennis_dry_run/tennis_eod_report.py tennis_dry_run/tests/test_shadow_outcomes_log.py
git commit -m "feat(tennis): persist resolved shadow outcomes to shadow_outcomes.jsonl"
```

---

## Phase 3 — Backfill historical outcomes from daily-report markdown

### Task 5: One-shot backfill script

**Files:**
- Create: `tennis_dry_run/tools/backfill_shadow_outcomes.py`
- Create: `tennis_dry_run/tests/test_backfill_shadow_outcomes.py`

**Approach:** Parse the `## Shadow Picks (tier B, 70-80% — not placed)` table from each `Daily-Reports/YYYY-MM-DD.md`. Columns vary (Outcome/Theo PnL appear only on EOD writes, T-90 columns appear when `shadow_placement` exists). Use a tolerant parser that locates the header row, identifies column indices, and ingests data rows with `WIN`/`LOSS`/`RETIRED` outcomes (skipping `pending` rows). Compute a stable `pick_id` from `(date, pick, opponent)` if the source row doesn't have one (it won't — the markdown doesn't carry pick_id). Use a `bf_` prefix to namespace backfill IDs and make them visibly distinct from live IDs.

Dedup against existing `shadow_outcomes.jsonl` by `(pick_id, status)`. Idempotent.

- [ ] **Step 1: Write failing tests.**

Create `tennis_dry_run/tests/test_backfill_shadow_outcomes.py`:

```python
"""Tests for backfill_shadow_outcomes — parses historical daily-report
markdown to seed shadow_outcomes.jsonl with pre-log outcomes."""

import json
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


def test_parse_shadow_table_extracts_resolved_rows(tmp_path):
    """parse_shadow_table returns one dict per WIN/LOSS/RETIRED row."""
    from tools.backfill_shadow_outcomes import parse_shadow_table
    from datetime import date

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


def test_parse_shadow_table_skips_pending_rows(tmp_path):
    """Pending rows are excluded — they'll be picked up by the live writer."""
    from tools.backfill_shadow_outcomes import parse_shadow_table
    from datetime import date

    md = SAMPLE_DAILY_REPORT.replace(
        "| Linda Noskova | Sara Errani | WTA Rome | clay | 0.7400 | 1.351 | 2026-05-12 09:00 | LOSS | $-25.00 |",
        "| Future Pick | X | ATP Rome | clay | 0.7400 | 1.351 | 2026-05-12 23:00 | pending | — |",
    )

    rows = parse_shadow_table(md, date(2026, 5, 12))

    pick_names = {r["pick"] for r in rows}
    assert "Future Pick" not in pick_names
    assert len(rows) == 3


def test_parse_shadow_table_handles_no_shadow_section(tmp_path):
    """A daily report without a Shadow Picks section returns []."""
    from tools.backfill_shadow_outcomes import parse_shadow_table
    from datetime import date

    rows = parse_shadow_table("# Tennis Daily Report — 2026-05-12\n\n_empty_\n", date(2026, 5, 12))
    assert rows == []


def test_parse_shadow_table_handles_no_picks_today(tmp_path):
    """The 'No shadow (tier B) picks today.' marker returns []."""
    from tools.backfill_shadow_outcomes import parse_shadow_table
    from datetime import date

    md = "# X\n\n## Shadow Picks (tier B, 70-80% — not placed)\n\n_No shadow (tier B) picks today._\n"
    rows = parse_shadow_table(md, date(2026, 5, 12))
    assert rows == []


def test_parse_shadow_table_handles_t90_columns(tmp_path):
    """Reports with T-90 columns mid-table still parse correctly."""
    from tools.backfill_shadow_outcomes import parse_shadow_table
    from datetime import date

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
    from datetime import date

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2026-05-12.md").write_text(SAMPLE_DAILY_REPORT, encoding="utf-8")

    log_file = tmp_path / "shadow_outcomes.jsonl"

    n1 = backfill_directory(vault, log_file)
    n2 = backfill_directory(vault, log_file)

    assert n1 == 4
    assert n2 == 0  # all already present, no double-append
    rows = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 4


def test_backfill_processes_multiple_daily_reports(tmp_path):
    """All YYYY-MM-DD.md files in the directory are processed."""
    from tools.backfill_shadow_outcomes import backfill_directory
    from datetime import date

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

    assert n == 8  # 4 per day × 2 days
    rows = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    dates = {r["game_time_iso"][:10] for r in rows}
    assert dates == {"2026-05-12", "2026-05-13"}
```

- [ ] **Step 2: Run to confirm they fail.**

```
python -m pytest tests/test_backfill_shadow_outcomes.py -v
```
Expected: 6 FAILs (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the backfill script.**

Create `tennis_dry_run/tools/backfill_shadow_outcomes.py`:

```python
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
EOD_HEADER_PREFIX = "## EOD Performance"
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


def parse_shadow_table(md: str, game_date: date) -> list[dict]:
    """Extract resolved shadow rows from a daily-report markdown body.

    Returns one dict per row with status in {WIN, LOSS, RETIRED}. Pending rows
    are skipped. If the report has no shadow section, or has the explicit
    "No shadow picks today" marker, returns [].
    """
    if SHADOW_HEADER not in md:
        return []
    start = md.index(SHADOW_HEADER)
    # Section ends at the next ## heading.
    rest = md[start + len(SHADOW_HEADER):]
    next_heading = rest.find("\n## ")
    section = rest if next_heading == -1 else rest[:next_heading]

    if NO_PICKS_MARKER in section:
        return []

    # Locate header row (starts with "| Pick"). Then the alignment row, then data.
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
        # Missing required column (e.g. Outcome) → nothing resolved.
        return []

    # Data rows: every | … | line after the alignment row (---|---).
    lines = section.splitlines()
    header_pos = next(i for i, ln in enumerate(lines) if ln.strip() == header_line)
    out: list[dict] = []
    for ln in lines[header_pos + 2:]:  # skip header + alignment
        ln = ln.strip()
        if not ln.startswith("|"):
            break  # table ended
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
```

- [ ] **Step 4: Run tests to confirm they pass.**

```
python -m pytest tests/test_backfill_shadow_outcomes.py -v
```
Expected: 6 PASSED.

- [ ] **Step 5: Commit.**

```
git add tennis_dry_run/tools/backfill_shadow_outcomes.py tennis_dry_run/tests/test_backfill_shadow_outcomes.py
git commit -m "feat(tennis): backfill_shadow_outcomes script for historical EOD markdown"
```

---

## Phase 4 — Cumulative shadow block in EOD report

### Task 6: `render_shadow_cumulative_block` renderer

**Files:**
- Modify: `tennis_dry_run/tennis_portfolio.py` — add renderer after `render_shadow_picks_block` (~line 410).
- Modify: `tennis_dry_run/tests/test_tennis_portfolio.py` — add tests.

The block reads `shadow_outcomes.jsonl`, dedups by `(pick_id, status)` taking the latest `resolved_at`, and produces:

```
## Shadow Performance (cumulative)

**Resolved: 47 | Wins: 23 | Win rate: 48.9% | Theoretical PnL: $-142.18**

### By model-prob bucket

| Bucket | Resolved | Wins | Hit rate | Predicted | Δ |
|---|---:|---:|---:|---:|---:|
| 0.70–0.75 | 18 | 8 | 44.4% | 72.5% | −28.1pp |
| 0.75–0.80 | 29 | 15 | 51.7% | 77.5% | −25.8pp |
```

- [ ] **Step 1: Write failing tests.**

Add to `tests/test_tennis_portfolio.py`:

```python
def test_shadow_cumulative_block_empty_log_returns_placeholder():
    """No outcomes log → friendly placeholder, not a missing section."""
    from tennis_portfolio import render_shadow_cumulative_block
    out = render_shadow_cumulative_block([])
    assert "## Shadow Performance (cumulative)" in out
    assert "_No resolved shadow outcomes yet._" in out


def test_shadow_cumulative_block_aggregates_win_loss_pnl():
    """Aggregate footer: count, wins, win rate %, total theoretical PnL."""
    from tennis_portfolio import render_shadow_cumulative_block
    rows = [
        {"pick_id": "0xa", "status": "WIN", "theoretical_pnl": 7.25, "model_prob": 0.77},
        {"pick_id": "0xb", "status": "LOSS", "theoretical_pnl": -25.0, "model_prob": 0.72},
        {"pick_id": "0xc", "status": "WIN", "theoretical_pnl": 9.0, "model_prob": 0.78},
        {"pick_id": "0xd", "status": "RETIRED", "theoretical_pnl": 0.0, "model_prob": 0.74},
    ]
    out = render_shadow_cumulative_block(rows)
    # RETIRED rows are excluded from W/L calc but counted as resolved? Convention:
    # exclude from win rate (mirrors render_shadow_picks_block which only counts
    # WIN/LOSS in the resolved aggregate).
    assert "**Resolved: 3" in out  # 2 W + 1 L; RETIRED excluded
    assert "Wins: 2" in out
    assert "Win rate: 66.7%" in out
    assert "Theoretical PnL: $-8.75" in out  # 7.25 - 25 + 9 = -8.75


def test_shadow_cumulative_block_renders_prob_buckets():
    """Bucket the resolved rows into model_prob bins and show hit rate vs predicted."""
    from tennis_portfolio import render_shadow_cumulative_block
    rows = [
        # 0.70-0.75 bucket: 1W, 2L (33% hit, ~72.5% predicted)
        {"pick_id": "0xa", "status": "WIN", "theoretical_pnl": 8.0, "model_prob": 0.72},
        {"pick_id": "0xb", "status": "LOSS", "theoretical_pnl": -25.0, "model_prob": 0.73},
        {"pick_id": "0xc", "status": "LOSS", "theoretical_pnl": -25.0, "model_prob": 0.74},
        # 0.75-0.80 bucket: 2W, 1L (66.7% hit, ~77.5% predicted)
        {"pick_id": "0xd", "status": "WIN", "theoretical_pnl": 8.0, "model_prob": 0.76},
        {"pick_id": "0xe", "status": "WIN", "theoretical_pnl": 8.0, "model_prob": 0.78},
        {"pick_id": "0xf", "status": "LOSS", "theoretical_pnl": -25.0, "model_prob": 0.79},
    ]
    out = render_shadow_cumulative_block(rows)
    assert "### By model-prob bucket" in out
    assert "0.70–0.75" in out
    assert "0.75–0.80" in out
    assert "33.3%" in out  # 0.70-0.75 hit rate
    assert "66.7%" in out  # 0.75-0.80 hit rate


def test_shadow_cumulative_block_dedupes_by_latest_resolved_at():
    """Two rows for the same pick_id: take the latest resolved_at (status flip
    from pending → WIN should not double-count)."""
    from tennis_portfolio import render_shadow_cumulative_block
    rows = [
        {"pick_id": "0xa", "status": "pending", "theoretical_pnl": 0.0, "model_prob": 0.77,
         "resolved_at": "2026-05-08T22:00:00+00:00"},
        {"pick_id": "0xa", "status": "WIN", "theoretical_pnl": 7.25, "model_prob": 0.77,
         "resolved_at": "2026-05-09T22:00:00+00:00"},
    ]
    out = render_shadow_cumulative_block(rows)
    assert "**Resolved: 1" in out
    assert "Wins: 1" in out
    assert "Theoretical PnL: $+7.25" in out
```

- [ ] **Step 2: Run them to confirm they fail.**

```
python -m pytest tests/test_tennis_portfolio.py -k shadow_cumulative -v
```
Expected: 4 FAILs (`ImportError` for `render_shadow_cumulative_block`).

- [ ] **Step 3: Implement the renderer.**

Add to `tennis_dry_run/tennis_portfolio.py` immediately after `render_shadow_picks_block` (after line 409):

```python
def render_shadow_cumulative_block(outcomes: list[dict]) -> str:
    """Render the cumulative shadow performance block from shadow_outcomes.jsonl.

    Dedup by pick_id taking the latest `resolved_at`. Reports aggregate W/L,
    win rate, theoretical PnL, plus model-prob calibration buckets so we can
    see at a glance whether the tier-B threshold is correctly placed.
    """
    if not outcomes:
        return (
            "## Shadow Performance (cumulative)\n\n"
            "_No resolved shadow outcomes yet._\n"
        )

    # Dedup: keep latest resolved_at per pick_id.
    latest: dict = {}
    for r in outcomes:
        pid = r.get("pick_id")
        if not pid:
            continue
        prev = latest.get(pid)
        if prev is None or r.get("resolved_at", "") > prev.get("resolved_at", ""):
            latest[pid] = r

    resolved = [r for r in latest.values() if r.get("status") in ("WIN", "LOSS")]
    wins = sum(1 for r in resolved if r["status"] == "WIN")
    total_pnl = sum(float(r.get("theoretical_pnl", 0.0)) for r in resolved)
    wr = (wins / len(resolved) * 100.0) if resolved else 0.0

    lines = ["## Shadow Performance (cumulative)", ""]
    lines.append(
        f"**Resolved: {len(resolved)} | Wins: {wins} | "
        f"Win rate: {wr:.1f}% | Theoretical PnL: {_money(total_pnl)}**"
    )

    if resolved:
        buckets = [(0.70, 0.75, "0.70–0.75"), (0.75, 0.80, "0.75–0.80")]
        bucket_rows = []
        for lo, hi, label in buckets:
            in_bucket = [r for r in resolved
                         if lo <= float(r.get("model_prob", 0.0)) < hi]
            if not in_bucket:
                continue
            b_wins = sum(1 for r in in_bucket if r["status"] == "WIN")
            b_hit = b_wins / len(in_bucket) * 100.0
            avg_pred = sum(float(r["model_prob"]) for r in in_bucket) / len(in_bucket) * 100.0
            delta = b_hit - avg_pred
            bucket_rows.append((label, len(in_bucket), b_wins, b_hit, avg_pred, delta))

        if bucket_rows:
            lines += [
                "",
                "### By model-prob bucket",
                "",
                "| Bucket | Resolved | Wins | Hit rate | Predicted | Δ |",
                "|---|---:|---:|---:|---:|---:|",
            ]
            for label, n, w, hit, pred, delta in bucket_rows:
                sign = "+" if delta >= 0 else ""
                lines.append(
                    f"| {label} | {n} | {w} | {hit:.1f}% | {pred:.1f}% | {sign}{delta:.1f}pp |"
                )

    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to confirm they pass.**

```
python -m pytest tests/test_tennis_portfolio.py -k shadow_cumulative -v
```
Expected: 4 PASSED.

- [ ] **Step 5: Commit.**

```
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "feat(tennis): render_shadow_cumulative_block — aggregate tier-B performance"
```

---

### Task 7: Wire cumulative block into `write_eod_report`

**Files:**
- Modify: `tennis_dry_run/tennis_eod_report.py:128-139, 223-239` — import + call.
- Modify: `tennis_dry_run/tests/test_tennis_eod_report.py` — add wiring test.

- [ ] **Step 1: Write the failing wiring test.**

Add to `tests/test_tennis_eod_report.py`:

```python
def test_eod_includes_cumulative_shadow_block_when_log_exists(tmp_path, monkeypatch):
    """When shadow_outcomes.jsonl exists, EOD section includes the cumulative
    block in addition to today's per-pick table."""
    from tennis_eod_report import write_eod_report
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr("tennis_dry_run.scrape_completed_results",
                        lambda target_date=None: [])

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")
    (state_dir / "skipped.jsonl").write_text("")
    (state_dir / "pending_selections.jsonl").write_text("")
    (state_dir / "shadow_selections.jsonl").write_text("")

    # Pre-seed the cumulative log with 3 resolved rows from prior days.
    (state_dir / "shadow_outcomes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            {"pick_id": "0xa", "status": "WIN", "theoretical_pnl": 7.25,
             "model_prob": 0.77, "resolved_at": "2026-05-12T22:00:00+00:00"},
            {"pick_id": "0xb", "status": "LOSS", "theoretical_pnl": -25.0,
             "model_prob": 0.73, "resolved_at": "2026-05-12T22:00:00+00:00"},
            {"pick_id": "0xc", "status": "WIN", "theoretical_pnl": 9.0,
             "model_prob": 0.78, "resolved_at": "2026-05-13T22:00:00+00:00"},
        ]) + "\n"
    )

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    out = write_eod_report(
        now_utc=datetime(2026, 5, 14, 22, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Shadow Performance (cumulative)" in body
    assert "**Resolved: 3" in body
    assert "Wins: 2" in body
    assert "Win rate: 66.7%" in body
```

- [ ] **Step 2: Run it (must fail).**

```
python -m pytest tests/test_tennis_eod_report.py::test_eod_includes_cumulative_shadow_block_when_log_exists -v
```
Expected: FAIL ("Shadow Performance (cumulative)" not in body).

- [ ] **Step 3: Wire the renderer into `write_eod_report`.**

In `tennis_eod_report.py`, add to the imports block (line 128-139):

```python
        render_shadow_picks_block,
        render_shadow_cumulative_block,
```

Then after the `render_shadow_picks_block(shadow_today)` line in the EOD section (~line 233), insert the cumulative block. Replace:

```python
        render_shadow_picks_block(shadow_today),
        render_placer_rejection_diagnostics_block(all_placer_skips, placed, now_utc),
```

with:

```python
        render_shadow_picks_block(shadow_today),
        render_shadow_cumulative_block(
            _read_jsonl(Path(state_dir) / "shadow_outcomes.jsonl")
        ),
        render_placer_rejection_diagnostics_block(all_placer_skips, placed, now_utc),
```

- [ ] **Step 4: Run the wiring test (must pass).**

```
python -m pytest tests/test_tennis_eod_report.py::test_eod_includes_cumulative_shadow_block_when_log_exists -v
```
Expected: PASS.

- [ ] **Step 5: Run the full eod-report test module + portfolio test module to verify no regressions.**

```
python -m pytest tests/test_tennis_eod_report.py tests/test_tennis_portfolio.py -v
```
Expected: all PASS.

- [ ] **Step 6: Run the entire tennis_dry_run test suite.**

```
python -m pytest tests/ -v
```
Expected: full pass (186+ existing tests + new ones added in this plan).

- [ ] **Step 7: Commit.**

```
git add tennis_dry_run/tennis_eod_report.py tennis_dry_run/tests/test_tennis_eod_report.py
git commit -m "feat(tennis): wire shadow cumulative block into EOD report"
```

---

## Phase 5 — VPS deploy + one-shot backfill (operations, not code)

### Task 8: Deploy + backfill on VPS

**This phase is manual ops; no code changes. The user runs these steps after the code phases land.**

- [ ] **Step 1: Sync to VPS.**

```
rsync -av --delete \
  --exclude '.tmp/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  "tennis_dry_run/" "vps:/opt/tennis-dry-run/"
```

- [ ] **Step 2: Run the test suite on VPS to confirm parity.**

```
ssh vps "cd /opt/tennis-dry-run && /opt/tennis-dry-run/venv/bin/python -m pytest tests/ -v"
```
Expected: same pass count as local.

- [ ] **Step 3: Run the backfill once.**

```
ssh vps "cd /opt/tennis-dry-run && /opt/tennis-dry-run/venv/bin/python tools/backfill_shadow_outcomes.py \
  --vault-dir /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports \
  --log-file /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl"
```
Expected output: `Appended N new row(s) to /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl` where N matches the manual count (4–8 rows across 2026-05-10..2026-05-13).

- [ ] **Step 4: Sanity check the log content.**

```
ssh vps "wc -l /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl && \
        cat /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl | head -5"
```

- [ ] **Step 5: Re-run backfill to confirm idempotency.**

```
ssh vps "cd /opt/tennis-dry-run && /opt/tennis-dry-run/venv/bin/python tools/backfill_shadow_outcomes.py \
  --vault-dir /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports \
  --log-file /opt/tennis-dry-run/.tmp/shadow_outcomes.jsonl"
```
Expected: `Appended 0 new row(s)`.

- [ ] **Step 6: Wait for the 22:00 UTC EOD cron to run and confirm the cumulative block appears in today's daily report.**

```
ssh vps "tail -200 /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Daily-Reports/$(date -u +%Y-%m-%d).md | grep -A 30 'Shadow Performance'"
```

- [ ] **Step 7: Update the project memory note.**

`memory/tennis_trading/project_shadow_picks_followup.md` — flip from open follow-up to "resolved: bug fixed + persistent log + cumulative block shipped on YYYY-MM-DD. Pending: calibration study after ~30 resolved picks accumulate."

Also update the research note at `vault/finance-brain/10-Projects/Tennis-Automated/shadow-picks-analysis-2026-05-15.md`:
- Mark Open Questions 2 and 3 as resolved.
- Annotate Open Questions 1 and 4 as "waiting for n≥30 resolved rows in shadow_outcomes.jsonl".

---

## Self-review checklist (run after writing the plan)

- [x] Spec coverage:
  - Open Q1 (calibration): deferred — depends on n≥30 (Phase 5 step 7 documents this).
  - Open Q2 (2026-05-11 bug): Tasks 1-3 — root cause identified, test reproduces, fix verified by integration test.
  - Open Q3 (persistent log): Tasks 4-5 — writer + backfill, idempotent.
  - Open Q4 (T-90 vs T-15 comparison): deferred — `shadow_outcomes.jsonl` retains `shadow_placement` only if upstream `resolve_shadow_outcomes` carries it (it does, via `**sel` spread); a future PR can slice the log by `would_place` vs `would_skip` once a calibration sample exists. Not in scope for this plan.
- [x] Placeholders: none.
- [x] Type/name consistency: `prune_shadow_stale` (matches Tasks 2 and 3); `append_shadow_outcomes_log` (Task 4); `render_shadow_cumulative_block` (Tasks 6 and 7); `parse_shadow_table` and `backfill_directory` (Task 5).
- [x] Each task includes test + impl + commit steps with exact commands.
