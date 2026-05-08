# Tennis Reporting Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cumulative running-state reporting to the tennis dry-run bot — daily forensic files plus a rolling dashboard file — with theoretical ¼-Kelly and ½-Kelly sizing tracks alongside the base flat-$25 bankroll.

**Architecture:** Two new pure modules (`tennis_kelly.py` for Kelly math + 3-bankroll replay, `tennis_portfolio.py` for markdown rendering). Existing `tennis_identifier.py` and `tennis_eod_report.py` become thin orchestration layers that call into these. All reports read from `state.json` + `trades.jsonl` + `pending_selections.jsonl` (no schema changes). Both crons fully re-render the rolling file (simpler than partial refresh — Closed Trades and Performance always reflect current `trades.jsonl`).

**Tech Stack:** Python 3.13, pytest 9, no new runtime deps.

**Reference:** [docs/superpowers/specs/2026-05-08-tennis-reporting-flow-design.md](../specs/2026-05-08-tennis-reporting-flow-design.md)

---

## File structure

**Create:**
- `tennis_dry_run/tennis_kelly.py` — pure math: `kelly_fraction`, `day_start_stake`, `replay_three_bankrolls`
- `tennis_dry_run/tennis_portfolio.py` — markdown rendering: 8 `render_*` functions
- `tennis_dry_run/tests/test_tennis_kelly.py` — Kelly + replay tests
- `tennis_dry_run/tests/test_tennis_portfolio.py` — render golden tests

**Modify:**
- `tennis_dry_run/tennis_identifier.py` — replace `write_vault_report` with calls to portfolio renderer; add rolling-file write
- `tennis_dry_run/tennis_eod_report.py` — replace `render_eod_section` with calls to portfolio renderer; add rolling-file write
- `tennis_dry_run/tests/test_tennis_identifier.py` — assert new BOD section shape + rolling-file write
- `tennis_dry_run/tests/test_tennis_eod_report.py` — assert new EOD section shape + rolling-file write

**No changes to:** `tennis_executor.py`, `tennis_placer.py`, `tennis_signing.py`, `tennis_sxbet.py`, `tennis_dry_run.py` (scan loop), executor service unit, cron entries.

---

## Task 0: Pre-flight — baseline-commit existing tennis_dry_run files

The branch was committed only with the `tennis_sxbet.py` fix; the rest of `tennis_dry_run/` is untracked. Bring it under version control as-is so subsequent diffs are clean.

**Files:**
- Track: everything currently untracked under `tennis_dry_run/` and `tests/`

- [ ] **Step 1: Verify untracked surface**

```bash
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas"
git status --short tennis_dry_run/
```
Expected: list of `??` entries for `tennis_identifier.py`, `tennis_placer.py`, `tennis_executor.py`, `tennis_eod_report.py`, `tennis_signing.py`, `tennis_dry_run.py`, `rebuild_player_profiles.py`, `__init__.py`, `requirements.txt`, `.env.example`, `tests/`, `tennis_model/`. Confirm no `.env` or `.tmp/` shows (these should be ignored by `.gitignore`).

- [ ] **Step 2: Stage the directory**

```bash
git add tennis_dry_run/
git status --short tennis_dry_run/
```
Expected: all entries now show `A` (added) prefix. No `??` lines.

- [ ] **Step 3: Confirm no sensitive files staged**

```bash
git diff --cached --name-only tennis_dry_run/ | grep -iE "\\.env$|credentials|token|\\.key$|\\.pem$" || echo "clean"
```
Expected: `clean`

- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
chore: baseline-commit tennis_dry_run package

The branch was started with the SX Bet param fix only; this brings the
rest of the dry-run package into version control as-is so subsequent
feature commits show clean diffs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Verify**

```bash
git log --oneline | head -5
git status --short tennis_dry_run/
```
Expected: 3 commits in log; `tennis_dry_run/` shows nothing untracked.

---

## Task 1: kelly_fraction

Pure function. Given win probability and decimal odds, return the Kelly fraction `f* = (p×odds - 1) / (odds - 1)`, clamped to `[0.0, 1.0]`.

**Files:**
- Create: `tennis_dry_run/tennis_kelly.py`
- Create: `tennis_dry_run/tests/test_tennis_kelly.py`

- [ ] **Step 1: Write failing test**

`tennis_dry_run/tests/test_tennis_kelly.py`:
```python
"""Tests for tennis_kelly — Kelly math and three-bankroll replay."""

import pytest

from tennis_kelly import kelly_fraction


def test_kelly_fraction_djokovic_example():
    # p=0.8713, odds=1.4953  →  f* = (0.8713*1.4953 - 1) / (1.4953 - 1)
    #                            = (1.3027 - 1) / 0.4953  =  0.6112
    assert kelly_fraction(prob=0.8713, decimal_odds=1.4953) == pytest.approx(0.6112, abs=0.0001)


def test_kelly_fraction_negative_edge_clamps_to_zero():
    # market 1.50 implies 66.7%; model says 50%  →  f* would be negative
    assert kelly_fraction(prob=0.50, decimal_odds=1.50) == 0.0


def test_kelly_fraction_zero_when_break_even():
    # p×odds == 1 exactly  →  f* = 0
    assert kelly_fraction(prob=0.5, decimal_odds=2.0) == 0.0


def test_kelly_fraction_clamps_at_one_for_max_input():
    # p=1.0 (sure win) gives f* = 1.0; never exceeds it
    assert kelly_fraction(prob=1.0, decimal_odds=2.0) == 1.0
    assert kelly_fraction(prob=1.0, decimal_odds=1.5) == 1.0
```

- [ ] **Step 2: Run, verify import-error fail**

```bash
cd tennis_dry_run
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: FAILED with `ModuleNotFoundError: No module named 'tennis_kelly'`.

- [ ] **Step 3: Write minimal impl**

`tennis_dry_run/tennis_kelly.py`:
```python
"""Kelly position sizing and three-bankroll replay for the tennis dry-run.

Pure functions — no I/O, no logging. Used by both tennis_identifier.py
and tennis_eod_report.py to compute Base / quarter-Kelly / half-Kelly
bankroll trajectories from the same trade journal.
"""

from __future__ import annotations


def kelly_fraction(prob: float, decimal_odds: float) -> float:
    """Compute the Kelly fraction for a binary back-bet.

    Formula: f* = (p × odds - 1) / (odds - 1), clamped to [0.0, 1.0].

    Args:
        prob: Model win probability in [0, 1].
        decimal_odds: Decimal odds (e.g. 1.50 = 2/1 fractional).

    Returns:
        Optimal fraction of bankroll to stake, clamped to [0, 1].
        Returns 0.0 when edge is non-positive (defensive — placer
        already filters negative_edge upstream).
    """
    if decimal_odds <= 1.0:
        return 0.0
    f_star = (prob * decimal_odds - 1.0) / (decimal_odds - 1.0)
    if f_star <= 0.0:
        return 0.0
    if f_star >= 1.0:
        return 1.0
    return f_star
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_kelly.py tennis_dry_run/tests/test_tennis_kelly.py
git commit -m "$(cat <<'EOF'
feat(tennis_kelly): add kelly_fraction with [0,1] clamp

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: day_start_stake with liquidity cap

Given a sizing mode, day-start balance, model prob, odds, and SX Bet liquidity, compute the actual stake (capped at liquidity) and a flag indicating whether capping occurred.

**Files:**
- Modify: `tennis_dry_run/tennis_kelly.py`
- Modify: `tennis_dry_run/tests/test_tennis_kelly.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_kelly.py`:
```python
from tennis_kelly import day_start_stake


def test_day_start_stake_base_mode_returns_flat_25():
    out = day_start_stake(
        mode="base",
        base_stake=25.0,
        kelly_multiplier=0.0,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out == {"stake": 25.0, "pre_cap_stake": 25.0, "capped": False}


def test_day_start_stake_quarter_kelly_uses_balance():
    # Djokovic: f* = 0.6112; 0.25 * f* * 500 = $76.40
    out = day_start_stake(
        mode="quarter_kelly",
        base_stake=25.0,
        kelly_multiplier=0.25,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out["stake"] == pytest.approx(76.40, abs=0.05)
    assert out["pre_cap_stake"] == pytest.approx(76.40, abs=0.05)
    assert out["capped"] is False


def test_day_start_stake_half_kelly_uses_balance():
    # 0.5 * 0.6112 * 500 = $152.81
    out = day_start_stake(
        mode="half_kelly",
        base_stake=25.0,
        kelly_multiplier=0.5,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=1000.0,
    )
    assert out["stake"] == pytest.approx(152.81, abs=0.05)
    assert out["capped"] is False


def test_day_start_stake_caps_to_liquidity():
    # half-Kelly wants $152.81 but book only has $39
    out = day_start_stake(
        mode="half_kelly",
        base_stake=25.0,
        kelly_multiplier=0.5,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=39.0,
    )
    assert out["stake"] == 39.0
    assert out["pre_cap_stake"] == pytest.approx(152.81, abs=0.05)
    assert out["capped"] is True


def test_day_start_stake_base_caps_to_liquidity_too():
    # Base $25 but book only has $10 (rare but possible)
    out = day_start_stake(
        mode="base",
        base_stake=25.0,
        kelly_multiplier=0.0,
        day_start_balance=500.0,
        prob=0.8713, decimal_odds=1.4953,
        liquidity_usd=10.0,
    )
    assert out == {"stake": 10.0, "pre_cap_stake": 25.0, "capped": True}


def test_day_start_stake_kelly_zero_on_negative_edge():
    # Kelly stake should be 0 when edge is negative
    out = day_start_stake(
        mode="quarter_kelly",
        base_stake=25.0,
        kelly_multiplier=0.25,
        day_start_balance=500.0,
        prob=0.50, decimal_odds=1.50,   # negative edge
        liquidity_usd=1000.0,
    )
    assert out == {"stake": 0.0, "pre_cap_stake": 0.0, "capped": False}
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: 6 new tests fail with `ImportError` on `day_start_stake`.

- [ ] **Step 3: Implement**

Append to `tennis_kelly.py`:
```python
def day_start_stake(
    *,
    mode: str,
    base_stake: float,
    kelly_multiplier: float,
    day_start_balance: float,
    prob: float,
    decimal_odds: float,
    liquidity_usd: float,
) -> dict:
    """Compute the actual stake for a sizing mode at placement time.

    Stake is locked at day-start balance for the mode (caller is
    responsible for passing the right value). If the computed stake
    exceeds the SX Bet available_usd at placement, it is capped to
    liquidity and the `capped` flag is set.

    Args:
        mode: "base" | "quarter_kelly" | "half_kelly". Used for
            documentation/debug only — the math is driven by
            kelly_multiplier and base_stake.
        base_stake: Flat-stake value used when kelly_multiplier == 0.
        kelly_multiplier: 0 for base, 0.25 for quarter-Kelly, 0.5 for
            half-Kelly. Larger values are accepted but clamped at the
            kelly_fraction step.
        day_start_balance: Bankroll for this mode at the start of the
            current UTC day.
        prob: Model win probability for the pick.
        decimal_odds: Odds being taken at placement.
        liquidity_usd: Available USD at the SX Bet price.

    Returns:
        {
          "stake": actual stake placed (≥ 0, ≤ liquidity_usd),
          "pre_cap_stake": stake before liquidity cap (for audit),
          "capped": True if liquidity cap reduced the stake,
        }
    """
    if kelly_multiplier <= 0.0:
        pre_cap = base_stake
    else:
        f_star = kelly_fraction(prob=prob, decimal_odds=decimal_odds)
        pre_cap = kelly_multiplier * f_star * day_start_balance

    if pre_cap <= 0.0:
        return {"stake": 0.0, "pre_cap_stake": 0.0, "capped": False}

    if pre_cap > liquidity_usd:
        return {"stake": liquidity_usd, "pre_cap_stake": pre_cap, "capped": True}
    return {"stake": pre_cap, "pre_cap_stake": pre_cap, "capped": False}
```

- [ ] **Step 4: Run, verify all pass**

```bash
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_kelly.py tennis_dry_run/tests/test_tennis_kelly.py
git commit -m "$(cat <<'EOF'
feat(tennis_kelly): day_start_stake with liquidity cap

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: replay_three_bankrolls

Walk a trade timeline (placed + settled) in chronological order, computing per-mode running balance, peak, drawdown, daily P&L list, capped-trade count, and current open-stake "Deployed" totals. The output is the single source of truth for every Portfolio metric.

**Files:**
- Modify: `tennis_dry_run/tennis_kelly.py`
- Modify: `tennis_dry_run/tests/test_tennis_kelly.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_kelly.py`:
```python
from datetime import datetime, timezone
from tennis_kelly import replay_three_bankrolls


def _open(pick_id, ts, prob, odds, avail, stake=25.0):
    return {
        "type": "open", "mode": "dry_run",
        "pick_id": pick_id, "pick": pick_id,
        "model_prob": prob, "sxbet_odds": odds,
        "sxbet_available_usd": avail, "stake": stake,
        "ts": ts,
    }


def _settled(pick_id, ts, won: bool, base_pnl: float):
    return {
        "type": "settled", "pick_id": pick_id,
        "won": won, "pnl": base_pnl,
        "ts": ts,
    }


def test_replay_empty_returns_starting_balance():
    out = replay_three_bankrolls(settled_trades=[], placed_trades=[], starting_balance=500.0)
    for mode in ("base", "quarter_kelly", "half_kelly"):
        assert out[mode]["balance"] == 500.0
        assert out[mode]["peak_balance"] == 500.0
        assert out[mode]["drawdown_pct"] == 0.0
        assert out[mode]["total_pnl"] == 0.0
        assert out[mode]["wins"] == 0
        assert out[mode]["losses"] == 0
        assert out[mode]["capped_count"] == 0


def test_replay_single_winning_trade():
    # Djokovic example: $25 base, $76.40 quarter-K, $152.81 half-K
    # Win at 1.4953 odds → profit = stake * (1.4953 - 1) = stake * 0.4953
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.38)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(512.38, abs=0.01)
    assert out["base"]["wins"] == 1
    assert out["quarter_kelly"]["balance"] == pytest.approx(537.83, abs=0.05)
    assert out["half_kelly"]["balance"] == pytest.approx(575.66, abs=0.05)


def test_replay_single_losing_trade():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=False, base_pnl=-25.0)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(475.0, abs=0.01)
    assert out["base"]["losses"] == 1
    assert out["quarter_kelly"]["balance"] == pytest.approx(423.60, abs=0.05)
    assert out["half_kelly"]["balance"] == pytest.approx(347.19, abs=0.05)


def test_replay_drawdown_resets_on_new_peak():
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", "2026-05-09T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("c", "2026-05-10T10:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True,  base_pnl=12.5),   # 500 → 512.50
        _settled("b", "2026-05-09T15:00:00+00:00", won=False, base_pnl=-25.0),  # 512.50 → 487.50, drawdown
        _settled("c", "2026-05-10T15:00:00+00:00", won=True,  base_pnl=12.5),   # 487.50 → 500.00, still drawdown vs 512.50
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["peak_balance"] == pytest.approx(512.50, abs=0.01)
    assert out["base"]["balance"] == pytest.approx(500.00, abs=0.01)
    # drawdown = (peak - bal) / peak = 12.5 / 512.5 = 2.439%
    assert out["base"]["drawdown_pct"] == pytest.approx(2.439, abs=0.01)


def test_replay_day_start_balance_carries_over():
    # Day 1: base balance 500 → 512.5 after win. Day 2 start balance = 512.5.
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", "2026-05-09T10:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.5),
        _settled("b", "2026-05-09T15:00:00+00:00", won=True, base_pnl=12.5),
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["balance"] == pytest.approx(525.0, abs=0.01)
    # quarter-K day 2 stake uses 512.5 + 12.5_quarter_pnl_day_1
    # f*(0.80, 1.50) = (1.20-1)/0.50 = 0.40
    # day 1 q-K stake = 0.25 * 0.40 * 500 = 50; pnl = 50 * 0.50 = +25
    # day 2 q-K start = 525; stake = 0.25 * 0.40 * 525 = 52.50; pnl = 52.50 * 0.50 = +26.25
    # final = 525 + 26.25 = 551.25
    assert out["quarter_kelly"]["balance"] == pytest.approx(551.25, abs=0.05)


def test_replay_capped_trade_uses_liquidity_for_stake():
    # half-K wants 152.81 on Djokovic but liquidity is 39.05 → capped
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 39.05)]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, base_pnl=12.38)]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    # half-Kelly stake capped at 39.05; pnl = 39.05 * 0.4953 = 19.34
    assert out["half_kelly"]["balance"] == pytest.approx(519.34, abs=0.05)
    assert out["half_kelly"]["capped_count"] == 1


def test_replay_today_pnl_and_today_roi():
    # Two trades on day 1 (today UTC), one win one loss
    today = datetime.now(timezone.utc).date().isoformat()
    placed = [
        _open("a", f"{today}T10:00:00+00:00", 0.80, 1.50, 1000.0),
        _open("b", f"{today}T11:00:00+00:00", 0.80, 1.50, 1000.0),
    ]
    settled = [
        _settled("a", f"{today}T15:00:00+00:00", won=True,  base_pnl=12.5),
        _settled("b", f"{today}T16:00:00+00:00", won=False, base_pnl=-25.0),
    ]
    out = replay_three_bankrolls(settled, placed, starting_balance=500.0)
    assert out["base"]["today_pnl"] == pytest.approx(-12.5, abs=0.01)
    # day_start_balance for today = 500 (no prior days)
    # today_roi = -12.5 / 500 = -2.5%
    assert out["base"]["today_roi_pct"] == pytest.approx(-2.5, abs=0.01)


def test_replay_deployed_sums_open_stake_per_mode():
    # One placed-but-not-yet-settled trade
    placed = [_open("djk", "2026-05-08T11:55:00+00:00", 0.8713, 1.4953, 1000.0)]
    out = replay_three_bankrolls(settled_trades=[], placed_trades=placed, starting_balance=500.0)
    assert out["base"]["deployed"] == 25.0
    assert out["quarter_kelly"]["deployed"] == pytest.approx(76.40, abs=0.05)
    assert out["half_kelly"]["deployed"] == pytest.approx(152.81, abs=0.05)
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: 8 new tests fail on `ImportError`.

- [ ] **Step 3: Implement**

Append to `tennis_kelly.py`:
```python
from datetime import datetime, date, timezone
from collections import defaultdict


_MODES = (
    ("base", 0.0),
    ("quarter_kelly", 0.25),
    ("half_kelly", 0.5),
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _utc_date(ts: str) -> date:
    return _parse_ts(ts).date()


def _empty_mode_state(starting_balance: float) -> dict:
    return {
        "balance": starting_balance,
        "peak_balance": starting_balance,
        "drawdown_pct": 0.0,
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "capped_count": 0,
        "today_pnl": 0.0,
        "today_roi_pct": 0.0,
        "today_start_balance": starting_balance,
        "deployed": 0.0,
        "open_stakes": {},  # pick_id -> stake (per mode); used internally
        "daily_pnl": defaultdict(float),  # date -> pnl (per mode)
        "daily_start_balance": {},  # date -> start-of-day balance (per mode)
    }


def replay_three_bankrolls(
    settled_trades: list[dict],
    placed_trades: list[dict],
    starting_balance: float = 500.0,
    today: date | None = None,
) -> dict:
    """Replay trade journal across three sizing modes.

    Walks all events (placed + settled) in chronological order. For each
    mode, records the day-start balance once per UTC day, computes the
    locked Kelly stake for each placed trade using that day-start balance,
    applies cap-to-liquidity, and updates running balance / peak /
    drawdown on settlement. Wins/losses come from the settled rows'
    `won` flag; per-mode pnl is recomputed (the journal's `pnl` is
    base-stake-only).

    Args:
        settled_trades: rows from trades.jsonl with type == "settled"
        placed_trades:  rows from trades.jsonl with type == "open"
        starting_balance: Day-0 balance for every mode (default $500).
        today: UTC date used for today_pnl / today_roi computation.
            Defaults to current UTC date. Tests pass an explicit value
            for determinism.

    Returns:
        {
          "base":          {balance, peak_balance, drawdown_pct, total_pnl,
                            wins, losses, capped_count, today_pnl,
                            today_roi_pct, today_start_balance, deployed,
                            avg_daily_roi_pct, daily_roi_history},
          "quarter_kelly": {... same shape ...},
          "half_kelly":    {... same shape ...},
        }
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    mode_state = {name: _empty_mode_state(starting_balance) for name, _ in _MODES}

    # Index settled by pick_id for win/loss lookup; we also need their ts
    # for sequencing. Build a unified event timeline: (ts, kind, row).
    events: list[tuple[datetime, str, dict]] = []
    for r in placed_trades:
        events.append((_parse_ts(r["ts"]), "placed", r))
    for r in settled_trades:
        events.append((_parse_ts(r["ts"]), "settled", r))
    events.sort(key=lambda x: x[0])

    # For each mode, track per-pick stake committed at placement.
    # On settlement, retire stake from `deployed` and apply pnl.

    for ts, kind, row in events:
        d = ts.date()

        for mode_name, kelly_mult in _MODES:
            ms = mode_state[mode_name]

            # Lazily record day-start balance for this date.
            if d not in ms["daily_start_balance"]:
                ms["daily_start_balance"][d] = ms["balance"]

            day_start_bal = ms["daily_start_balance"][d]

            if kind == "placed":
                stake_info = day_start_stake(
                    mode=mode_name,
                    base_stake=float(row.get("stake", 25.0)),
                    kelly_multiplier=kelly_mult,
                    day_start_balance=day_start_bal,
                    prob=float(row["model_prob"]),
                    decimal_odds=float(row["sxbet_odds"]),
                    liquidity_usd=float(row["sxbet_available_usd"]),
                )
                ms["open_stakes"][row["pick_id"]] = stake_info
                ms["deployed"] += stake_info["stake"]
                if stake_info["capped"]:
                    ms["capped_count"] += 1

            else:  # settled
                pick_id = row["pick_id"]
                stake_info = ms["open_stakes"].pop(pick_id, None)
                if stake_info is None:
                    continue  # orphaned settle row (open removed from state.json)

                stake = stake_info["stake"]
                won = bool(row.get("won", False))

                # Recompute per-mode pnl from this mode's stake.
                # Pull the matching `placed` row for the odds the trade
                # entered at — settlement row may not carry odds.
                placed_row = next(
                    (p for p in placed_trades if p["pick_id"] == pick_id), None
                )
                if placed_row is None:
                    continue
                odds = float(placed_row["sxbet_odds"])
                pnl = stake * (odds - 1.0) if won else -stake

                ms["balance"] += pnl
                ms["total_pnl"] += pnl
                ms["deployed"] -= stake
                ms["daily_pnl"][d] += pnl

                if won:
                    ms["wins"] += 1
                else:
                    ms["losses"] += 1

                if ms["balance"] > ms["peak_balance"]:
                    ms["peak_balance"] = ms["balance"]

                if ms["peak_balance"] > 0:
                    ms["drawdown_pct"] = max(
                        0.0,
                        (ms["peak_balance"] - ms["balance"]) / ms["peak_balance"] * 100.0,
                    )

    # Derive today_pnl / today_roi / avg_daily_roi for each mode.
    for mode_name, _ in _MODES:
        ms = mode_state[mode_name]
        today_start = ms["daily_start_balance"].get(today, ms["balance"])
        today_pnl = ms["daily_pnl"].get(today, 0.0)
        ms["today_pnl"] = today_pnl
        ms["today_start_balance"] = today_start
        ms["today_roi_pct"] = (
            (today_pnl / today_start) * 100.0 if today_start else 0.0
        )

        # avg_daily_roi: mean of (daily_pnl[d] / daily_start[d]) over days with activity
        rois = []
        for d, pnl in ms["daily_pnl"].items():
            start = ms["daily_start_balance"].get(d, starting_balance)
            if start:
                rois.append(pnl / start * 100.0)
        ms["avg_daily_roi_pct"] = sum(rois) / len(rois) if rois else 0.0
        ms["daily_roi_history"] = sorted(
            [
                (d.isoformat(), pnl, ms["daily_start_balance"].get(d, starting_balance))
                for d, pnl in ms["daily_pnl"].items()
            ]
        )

    # Strip internals from public output.
    for ms in mode_state.values():
        ms.pop("open_stakes", None)
        ms.pop("daily_pnl", None)
        ms.pop("daily_start_balance", None)

    return mode_state
```

- [ ] **Step 4: Run, verify all pass**

```bash
python -m pytest tests/test_tennis_kelly.py -p no:anchorpy -v
```
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_kelly.py tennis_dry_run/tests/test_tennis_kelly.py
git commit -m "$(cat <<'EOF'
feat(tennis_kelly): replay_three_bankrolls timeline replay

Walks placed + settled trades chronologically, applies day-start-locked
Kelly stake per mode with liquidity caps, tracks balance / peak /
drawdown / daily P&L for Base, 1/4-Kelly, 1/2-Kelly bankrolls.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: render_portfolio_block

The 11-row, 3-column markdown table reused across daily and rolling files.

**Files:**
- Create: `tennis_dry_run/tennis_portfolio.py`
- Create: `tennis_dry_run/tests/test_tennis_portfolio.py`

- [ ] **Step 1: Write failing test**

`tennis_dry_run/tests/test_tennis_portfolio.py`:
```python
"""Tests for tennis_portfolio — markdown render functions."""

from datetime import datetime, timezone, date

import pytest

from tennis_kelly import replay_three_bankrolls
from tennis_portfolio import render_portfolio_block


def _open(pid, ts, prob=0.8713, odds=1.4953, avail=1000.0, stake=25.0):
    return {
        "type": "open", "mode": "dry_run",
        "pick_id": pid, "pick": pid,
        "model_prob": prob, "sxbet_odds": odds,
        "sxbet_available_usd": avail, "stake": stake,
        "ts": ts,
    }


def _settled(pid, ts, won, pnl):
    return {"type": "settled", "pick_id": pid, "won": won, "pnl": pnl, "ts": ts}


def test_portfolio_block_zero_state():
    replay = replay_three_bankrolls(
        settled_trades=[], placed_trades=[], starting_balance=500.0,
        today=date(2026, 5, 8),
    )
    out = render_portfolio_block(
        replay, datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        base_stake_usd=25.0,
    )
    assert "### Portfolio (snapshot 2026-05-08 07:00 UTC)" in out
    assert "| Balance         | $500.00 | $500.00 | $500.00 |" in out
    assert "| Total P&L       | $+0.00  | $+0.00  | $+0.00  |" in out
    assert "| Today ROI       | +0.00%  | +0.00%  | +0.00%  |" in out


def test_portfolio_block_after_winning_trade():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00")]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed, today=date(2026, 5, 8))
    out = render_portfolio_block(
        replay, datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        base_stake_usd=25.0,
    )
    # Base: 500 → 512.38
    assert "| Balance         | $512.38 |" in out
    # quarter-K: 500 → 537.83 ish
    assert "$537." in out
    # half-K: 500 → 575.66 ish
    assert "$575." in out
    assert "| Today P&L       | $+12.38" in out
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: ImportError on `tennis_portfolio`.

- [ ] **Step 3: Implement**

`tennis_dry_run/tennis_portfolio.py`:
```python
"""Markdown rendering for tennis dry-run reports.

Pure functions — given replay output and inputs, return strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable


def _money(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"${sign}{abs(v):.2f}"


def _money_abs(v: float) -> str:
    return f"${v:.2f}"


def _pct(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v):.2f}%"


def _row(metric: str, base: str, qk: str, hk: str) -> str:
    return f"| {metric:<15} | {base:<7} | {qk:<7} | {hk:<7} |"


def render_portfolio_block(
    replay: dict,
    now_utc: datetime,
    *,
    base_stake_usd: float = 25.0,
) -> str:
    """Render the Portfolio table — 3 columns (Base / 1/4 K / 1/2 K).

    Args:
        replay: output of tennis_kelly.replay_three_bankrolls
        now_utc: timestamp shown in the header
        base_stake_usd: flat base stake (shown in "Today's Stake" row)
    """
    b = replay["base"]
    q = replay["quarter_kelly"]
    h = replay["half_kelly"]
    ts = now_utc.strftime("%Y-%m-%d %H:%M")

    # "Today's Stake" — the day-start-locked stake for each mode for today.
    # Base is the flat base stake. Kelly stakes computed off today_start_balance.
    # We don't have prob/odds at portfolio level, so show the "would-have-been
    # stake at full-Kelly multiplier × today_start_balance" placeholder is meaningless.
    # Instead show: "$X*" where * is "{stake_pct}% of bankroll" footnote.
    # For simplicity we display: base = base_stake; KK = 0.25 × today_start; HK = 0.5 × today_start
    # NB: this is the BANKROLL share, not multiplied by f*. f* depends on the pick.
    # We render this as the deployed-share view rather than per-pick stake.
    # Today's Stake row shows the "max possible day-start stake" (= multiplier × balance),
    # which collapses to the day-start balance for full-Kelly.
    qk_today_max = 0.25 * q["today_start_balance"]
    hk_today_max = 0.5 * h["today_start_balance"]

    lines = [
        f"### Portfolio (snapshot {ts} UTC)",
        "",
        "| Metric          | Base    | ¼ Kelly | ½ Kelly |",
        "|---|---:|---:|---:|",
        _row("Balance",       _money_abs(b["balance"]),       _money_abs(q["balance"]),       _money_abs(h["balance"])),
        _row("Starting",      _money_abs(500.0),              _money_abs(500.0),              _money_abs(500.0)),
        _row("Total P&L",     _money(b["total_pnl"]),         _money(q["total_pnl"]),         _money(h["total_pnl"])),
        _row("Today P&L",     _money(b["today_pnl"]),         _money(q["today_pnl"]),         _money(h["today_pnl"])),
        _row("Today ROI",     _pct(b["today_roi_pct"]),       _pct(q["today_roi_pct"]),       _pct(h["today_roi_pct"])),
        _row("Avg Daily ROI", _pct(b["avg_daily_roi_pct"]),   _pct(q["avg_daily_roi_pct"]),   _pct(h["avg_daily_roi_pct"])),
        _row("Peak Balance",  _money_abs(b["peak_balance"]),  _money_abs(q["peak_balance"]),  _money_abs(h["peak_balance"])),
        _row("Drawdown",      f"{b['drawdown_pct']:.2f}%",    f"{q['drawdown_pct']:.2f}%",    f"{h['drawdown_pct']:.2f}%"),
        _row("Deployed",      _money_abs(b["deployed"]),      _money_abs(q["deployed"]),      _money_abs(h["deployed"])),
        _row("Today's Stake", _money_abs(base_stake_usd),     _money_abs(qk_today_max),       _money_abs(hk_today_max)),
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "$(cat <<'EOF'
feat(tennis_portfolio): render_portfolio_block

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: render_open_picks_block + render_closed_trades_block

Two related tables: currently-open picks (state.open_picks) and recent settled trades (last N from trades.jsonl).

**Files:**
- Modify: `tennis_dry_run/tennis_portfolio.py`
- Modify: `tennis_dry_run/tests/test_tennis_portfolio.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_portfolio.py`:
```python
from tennis_portfolio import render_open_picks_block, render_closed_trades_block


def test_open_picks_block_empty_state():
    out = render_open_picks_block(open_picks={}, replay={
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    })
    assert "_No open picks._" in out


def test_open_picks_block_one_pick():
    open_picks = {
        "0xabc": {
            "pick_id": "0xabc", "pick": "Novak Djokovic",
            "opponent": "Dino Prizmic",
            "league": "ATP Rome", "model_prob": 0.8713,
            "sxbet_odds": 1.4953, "sxbet_available_usd": 1000.0,
            "edge": 0.2026, "stake": 25.0,
            "ts": "2026-05-08T11:55:00+00:00",
        }
    }
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_open_picks_block(open_picks, replay)
    assert "Novak Djokovic" in out
    assert "Dino Prizmic" in out
    assert "1.495" in out
    assert "+20.3%" in out
    # base $25, ¼K $76.40, ½K $152.81
    assert "$25.00" in out
    assert "$76." in out
    assert "$152." in out


def test_closed_trades_block_empty():
    out = render_closed_trades_block(settled=[], placed=[], n=30)
    assert "_No closed trades yet._" in out


def test_closed_trades_block_orders_newest_first():
    placed = [
        _open("a", "2026-05-06T11:00:00+00:00", odds=1.50, stake=25.0),
        _open("b", "2026-05-08T11:00:00+00:00", odds=1.50, stake=25.0),
    ]
    settled = [
        _settled("a", "2026-05-06T15:00:00+00:00", won=True,  pnl=12.5),
        _settled("b", "2026-05-08T15:00:00+00:00", won=False, pnl=-25.0),
    ]
    out = render_closed_trades_block(settled=settled, placed=placed, n=30)
    # Newest (2026-05-08) row appears before older (2026-05-06)
    idx_b = out.index("2026-05-08")
    idx_a = out.index("2026-05-06")
    assert idx_b < idx_a
    assert "WIN" in out
    assert "LOSS" in out


def test_closed_trades_block_respects_n_limit():
    placed = [_open(f"p{i}", f"2026-05-{i+1:02d}T11:00:00+00:00") for i in range(5)]
    settled = [_settled(f"p{i}", f"2026-05-{i+1:02d}T15:00:00+00:00", won=True, pnl=12.38) for i in range(5)]
    out = render_closed_trades_block(settled=settled, placed=placed, n=2)
    # Only 2 newest dates appear in the body rows
    assert "2026-05-05" in out  # newest two: 05 and 04
    assert "2026-05-04" in out
    assert "2026-05-01" not in out
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: ImportError on the two new functions.

- [ ] **Step 3: Implement**

Append to `tennis_portfolio.py`:
```python
def render_open_picks_block(open_picks: dict, replay: dict) -> str:
    """Render currently-open picks table (one row per state.open_picks entry).

    Stake columns show what each mode would have committed at placement,
    using each mode's current today_start_balance from the replay output
    as the locked stake.
    """
    if not open_picks:
        return "### Open Picks (0)\n\n_No open picks._\n"

    from tennis_kelly import day_start_stake

    lines = [
        f"### Open Picks ({len(open_picks)})",
        "",
        "| Pick | Opponent | Match (UTC) | League | Entry odds | Edge | Base | ¼K | ½K |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for pick_id, p in open_picks.items():
        prob = float(p["model_prob"])
        odds = float(p["sxbet_odds"])
        avail = float(p.get("sxbet_available_usd", 0.0))
        edge = float(p.get("edge", prob - 1.0 / odds))
        match_time = p.get("ts", "")[:19].replace("T", " ")

        base = day_start_stake(
            mode="base", base_stake=25.0, kelly_multiplier=0.0,
            day_start_balance=replay["base"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )
        qk = day_start_stake(
            mode="quarter_kelly", base_stake=25.0, kelly_multiplier=0.25,
            day_start_balance=replay["quarter_kelly"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )
        hk = day_start_stake(
            mode="half_kelly", base_stake=25.0, kelly_multiplier=0.5,
            day_start_balance=replay["half_kelly"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )

        lines.append(
            f"| {p.get('pick','?')} | {p.get('opponent','?')} | {match_time} | "
            f"{p.get('league','?')} | {odds:.3f} | {_pct(edge*100)} | "
            f"{_money_abs(base['stake'])} | {_money_abs(qk['stake'])} | "
            f"{_money_abs(hk['stake'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_closed_trades_block(
    settled: list[dict],
    placed: list[dict],
    n: int = 30,
) -> str:
    """Render the most recent `n` settled trades, newest first.

    For each settled row, look up the matching placed row by pick_id to
    pull entry odds + stake. P&L per mode is recomputed from the
    per-mode stake (which is in the placed row only after we re-replay,
    but for display we approximate by recomputing from odds + won flag
    using the base stake in the placed row scaled by mode multiplier).

    For accurate per-mode P&L, the caller passes the replay output via
    the `replay` parameter (added in Task 6 if needed). For now we use
    base stake × multiplier × (odds-1) for wins, -stake for losses.

    NB: This is a near-correct display value; the Performance block
    aggregates from the canonical replay numbers.
    """
    if not settled:
        return "### Closed Trades (0)\n\n_No closed trades yet._\n"

    placed_by_id = {p["pick_id"]: p for p in placed}
    rows = []
    for s in settled:
        pid = s["pick_id"]
        p = placed_by_id.get(pid)
        if p is None:
            continue
        ts = s.get("ts", "")[:10]
        odds = float(p["sxbet_odds"])
        won = bool(s.get("won", False))
        base_stake = float(p.get("stake", 25.0))
        # Per-mode display stakes
        # NB: these are approximations using base stake's multiplier
        # without day-start-balance compounding; canonical numbers
        # come from the replay output for the Performance block.
        b_pnl = base_stake * (odds - 1.0) if won else -base_stake
        rows.append((ts, p, s, odds, won, b_pnl))

    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:n]

    lines = [
        f"### Closed Trades (last {min(n, len(rows))}, newest first)",
        "",
        "| Date | Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L | Result |",
        "|---|---|---|---:|---|---:|---:|---:|---|",
    ]
    for ts, p, s, odds, won, b_pnl in rows:
        result = "WIN" if won else "LOSS"
        outcome = "✓" if won else "✗"
        # Approximate Kelly P&L: same formula but stake = base × (multiplier × f* / base_pct).
        # To avoid depending on day-start-balance per row, we scale by
        # the ratio of (kelly day-start stake / base stake) from the
        # CURRENT replay's deployed values.
        # Simpler: just show base P&L and Kelly P&L = base × (multiplier × f* × bal_at_time / base_stake).
        # For display purposes, let's use Kelly P&L = base_pnl × (kelly_stake_at_time / base_stake_at_time).
        # We derive kelly_stake_at_time from the original placed row's available_usd cap.
        prob = float(p["model_prob"])
        from tennis_kelly import kelly_fraction
        f = kelly_fraction(prob=prob, decimal_odds=odds)
        avail = float(p.get("sxbet_available_usd", 0.0))
        # approximate balance at time of trade as starting balance — Performance block has the canonical.
        qk_stake = min(0.25 * f * 500.0, avail) if f > 0 else 0.0
        hk_stake = min(0.5 * f * 500.0, avail) if f > 0 else 0.0
        qk_pnl = qk_stake * (odds - 1.0) if won else -qk_stake
        hk_pnl = hk_stake * (odds - 1.0) if won else -hk_stake

        lines.append(
            f"| {ts} | {p.get('pick','?')} | {p.get('opponent','?')} | "
            f"{odds:.3f} | {outcome} | "
            f"{_money(b_pnl)} | {_money(qk_pnl)} | {_money(hk_pnl)} | {result} |"
        )
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "$(cat <<'EOF'
feat(tennis_portfolio): open picks and closed trades blocks

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: render_performance_block + render_backtest_comparison_block

Cumulative metrics across all settled trades for each of the three modes, plus a side-by-side vs the backtested 87.4% / 4.40 PF.

**Files:**
- Modify: `tennis_dry_run/tennis_portfolio.py`
- Modify: `tennis_dry_run/tests/test_tennis_portfolio.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_portfolio.py`:
```python
from tennis_portfolio import render_performance_block, render_backtest_comparison_block


def test_performance_block_zero_trades():
    replay = replay_three_bankrolls(settled_trades=[], placed_trades=[])
    out = render_performance_block(replay)
    assert "_No closed trades yet._" in out or "| Total trades      | 0" in out


def test_performance_block_with_one_win():
    placed = [_open("djk", "2026-05-08T11:55:00+00:00")]
    settled = [_settled("djk", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed)
    out = render_performance_block(replay)
    assert "| Total trades      | 1" in out
    assert "100.00%" in out  # win rate
    assert "$12.38" in out
    assert "$+12.38" in out  # avg pnl


def test_performance_block_mixed():
    placed = [
        _open("a", "2026-05-08T10:00:00+00:00"),
        _open("b", "2026-05-08T11:00:00+00:00"),
        _open("c", "2026-05-08T12:00:00+00:00"),
    ]
    settled = [
        _settled("a", "2026-05-08T15:00:00+00:00", won=True,  pnl=12.38),
        _settled("b", "2026-05-08T16:00:00+00:00", won=False, pnl=-25.0),
        _settled("c", "2026-05-08T17:00:00+00:00", won=True,  pnl=12.38),
    ]
    replay = replay_three_bankrolls(settled, placed)
    out = render_performance_block(replay)
    assert "| Total trades      | 3" in out
    assert "66.67%" in out  # 2/3 win rate
    assert "2 / 1" in out   # wins / losses


def test_backtest_comparison_block():
    placed = [_open("a", "2026-05-08T10:00:00+00:00")]
    settled = [_settled("a", "2026-05-08T15:00:00+00:00", won=True, pnl=12.38)]
    replay = replay_three_bankrolls(settled, placed)
    out = render_backtest_comparison_block(replay)
    assert "Backtest" in out
    assert "87.4%" in out  # backtest win rate
    assert "11,161" in out  # backtest sample size
    assert "100.00%" in out  # actual base win rate
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```

- [ ] **Step 3: Implement**

Append to `tennis_portfolio.py`:
```python
def _profit_factor(wins_sum: float, losses_sum: float) -> str:
    """profit factor = sum(wins) / |sum(losses)|. Render '∞' on no losses."""
    if losses_sum == 0:
        return "∞" if wins_sum > 0 else "n/a"
    return f"{wins_sum / abs(losses_sum):.2f}"


def render_performance_block(replay: dict) -> str:
    """Render cumulative performance table (Base / 1/4 K / 1/2 K)."""
    b, q, h = replay["base"], replay["quarter_kelly"], replay["half_kelly"]

    total = b["wins"] + b["losses"]
    if total == 0:
        return "### Performance (cumulative)\n\n_No closed trades yet._\n"

    win_rate = (b["wins"] / total) * 100.0

    # Per-mode avg pnl, profit factor, largest win/loss are not in the
    # replay's public surface — recompute here from total_pnl.
    # For a simple v1, expose total_pnl / total and skip per-trade
    # min/max (covered in v2 if needed).
    avg_b = b["total_pnl"] / total
    avg_q = q["total_pnl"] / total
    avg_h = h["total_pnl"] / total

    lines = [
        "### Performance (cumulative)",
        "",
        "| Metric            | Base    | ¼ Kelly | ½ Kelly |",
        "|---|---:|---:|---:|",
        _row("Total trades",     str(total),                   str(total),                   str(total)),
        _row("Wins / Losses",    f"{b['wins']} / {b['losses']}", f"{b['wins']} / {b['losses']}", f"{b['wins']} / {b['losses']}"),
        _row("Win rate",         f"{win_rate:.2f}%",           f"{win_rate:.2f}%",           f"{win_rate:.2f}%"),
        _row("Avg P&L/trade",    _money(avg_b),                _money(avg_q),                _money(avg_h)),
        _row("Total P&L",        _money(b["total_pnl"]),       _money(q["total_pnl"]),       _money(h["total_pnl"])),
        _row("Max drawdown",     f"{b['drawdown_pct']:.2f}%",  f"{q['drawdown_pct']:.2f}%",  f"{h['drawdown_pct']:.2f}%"),
        _row("Liquidity-capped", str(b["capped_count"]),       str(q["capped_count"]),       str(h["capped_count"])),
        "",
    ]
    return "\n".join(lines)


def render_backtest_comparison_block(replay: dict) -> str:
    """Render backtest-vs-actual comparison table.

    Backtest reference values come from the optimal filter config and
    walk-forward results captured in memory: 87.4% SR, PF 4.40, 11,161
    sampled matches at the 80%+ confidence + odds<=2.00 threshold.
    """
    b, q, h = replay["base"], replay["quarter_kelly"], replay["half_kelly"]
    total = b["wins"] + b["losses"]
    if total == 0:
        actual_wr_base = "n/a"
    else:
        actual_wr_base = f"{(b['wins'] / total) * 100.0:.2f}%"

    lines = [
        "### Backtest vs Dry Run",
        "",
        "| Metric          | Backtest | Base    | ¼ Kelly | ½ Kelly |",
        "|---|---:|---:|---:|---:|",
        f"| Win rate        | 87.4%    | {actual_wr_base:>7} | {actual_wr_base:>7} | {actual_wr_base:>7} |",
        f"| Sample size     | 11,161   | {total:>7} | {total:>7} | {total:>7} |",
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "$(cat <<'EOF'
feat(tennis_portfolio): performance and backtest comparison blocks

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: render_identified_picks_block (BOD daily file table)

The qualified-only "Identified Picks" table for the daily file, with the new SX Bet odds / Edge / Liquidity columns now that the param-name fix is in.

**Files:**
- Modify: `tennis_dry_run/tennis_portfolio.py`
- Modify: `tennis_dry_run/tests/test_tennis_portfolio.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_portfolio.py`:
```python
from tennis_portfolio import render_identified_picks_block


def test_identified_picks_block_empty():
    out = render_identified_picks_block(selections=[])
    assert "_No qualifying selections today._" in out


def test_identified_picks_block_with_real_odds():
    selections = [
        {
            "pick": "Iga Swiatek", "opponent": "Catherine McNally",
            "league": "WTA Rome", "surface": "clay",
            "model_prob": 0.8848, "fair_odds": 1.130,
            "sxbet_odds": None,            # no liquidity at 07:00
            "sxbet_available_usd": None,
            "edge": None,
            "game_time_iso": "2026-05-08T09:00:00+00:00",
            "placement_path": "scheduled", "scheduled_at_iso": "2026-05-08T08:45:00+00:00",
        },
        {
            "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
            "league": "ATP Rome", "surface": "clay",
            "model_prob": 0.8713, "fair_odds": 1.148,
            "sxbet_odds": 1.5534,
            "sxbet_available_usd": 39.05,
            "edge": 0.226,
            "game_time_iso": "2026-05-08T12:10:00+00:00",
            "placement_path": "scheduled", "scheduled_at_iso": "2026-05-08T11:55:00+00:00",
        },
    ]
    out = render_identified_picks_block(selections)
    assert "Iga Swiatek" in out
    assert "Catherine McNally" in out
    assert "1.553" in out  # Djokovic
    assert "+22.60%" in out
    assert "$39.05" in out
    assert "—" in out  # Swiatek's blank cells
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```

- [ ] **Step 3: Implement**

Append to `tennis_portfolio.py`:
```python
def render_identified_picks_block(selections: list[dict]) -> str:
    """Render the BOD 'Identified Picks' table (qualified picks only).

    Args:
        selections: list of dicts with keys pick, opponent, league,
            surface, model_prob, fair_odds, sxbet_odds (None if no
            liquidity), sxbet_available_usd, edge, game_time_iso,
            placement_path, scheduled_at_iso.
    """
    if not selections:
        return "## Identified Picks\n\n_No qualifying selections today._\n"

    lines = [
        "## Identified Picks",
        "",
        "| Pick | Opponent | League | Surface | Model Prob | Fair Odds | "
        "SX Bet @07:00 | Edge | Liquidity | Match (UTC) | Placement |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for s in selections:
        sx = s.get("sxbet_odds")
        avail = s.get("sxbet_available_usd")
        edge = s.get("edge")
        match_time = (s.get("game_time_iso") or "")[:16].replace("T", " ")
        sched = (s.get("scheduled_at_iso") or "")[11:16]
        placement = s.get("placement_path", "?")
        if placement == "scheduled" and sched:
            placement = f"scheduled {sched}"

        lines.append(
            f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
            f"{s.get('league','?')} | {s.get('surface','?')} | "
            f"{float(s.get('model_prob', 0)):.4f} | "
            f"{float(s.get('fair_odds', 0)):.3f} | "
            f"{(f'{sx:.3f}' if sx else '—'):>5} | "
            f"{(_pct(edge*100) if edge is not None else '—'):>7} | "
            f"{(_money_abs(avail) if avail else '—'):>9} | "
            f"{match_time} | {placement} |"
        )
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "$(cat <<'EOF'
feat(tennis_portfolio): identified picks block (BOD)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: EOD-only render functions — placer activity + settlements

Two daily-file-only EOD tables: today's placer fires (placed + skipped, with stakes per mode) and today's settlements (P&L per mode).

**Files:**
- Modify: `tennis_dry_run/tennis_portfolio.py`
- Modify: `tennis_dry_run/tests/test_tennis_portfolio.py`

- [ ] **Step 1: Add failing tests**

Append to `test_tennis_portfolio.py`:
```python
from tennis_portfolio import (
    render_today_placer_activity_block,
    render_today_settlements_block,
)


def test_today_placer_activity_empty():
    out = render_today_placer_activity_block(placed_today=[], placer_skips=[], replay={
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    })
    assert "_No placer activity today._" in out


def test_today_placer_activity_mixed():
    placed = [{
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "model_prob": 0.8713, "sxbet_odds": 1.4953,
        "sxbet_available_usd": 1000.0, "edge": 0.2026,
        "stake": 25.0, "ts": "2026-05-08T11:55:00+00:00",
    }]
    skipped = [{
        "pick": "Alexander Zverev", "opponent": "Daniel Altmaier",
        "league": "ATP Rome", "sxbet_odds": 1.087,
        "edge": -0.063, "reason": "negative_edge",
        "ts": "2026-05-08T10:45:00+00:00",
    }]
    replay = {
        "base": {"today_start_balance": 500.0},
        "quarter_kelly": {"today_start_balance": 500.0},
        "half_kelly": {"today_start_balance": 500.0},
    }
    out = render_today_placer_activity_block(placed_today=placed, placer_skips=skipped, replay=replay)
    assert "Djokovic" in out
    assert "placed" in out
    assert "Zverev" in out
    assert "skipped: negative_edge" in out
    assert "$25.00" in out  # Djokovic base
    assert "$76." in out    # Djokovic ¼K
    assert "$152." in out   # Djokovic ½K
    assert "—" in out       # Zverev stake columns


def test_today_settlements_empty():
    out = render_today_settlements_block(settlements=[], placed_lookup={})
    assert "_No settlements today._" in out


def test_today_settlements_winning():
    settlements = [{
        "pick_id": "0xdjk",
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "won": True, "pnl": 12.38,
        "ts": "2026-05-08T15:00:00+00:00",
    }]
    placed = {"0xdjk": {
        "pick_id": "0xdjk", "model_prob": 0.8713,
        "sxbet_odds": 1.4953, "sxbet_available_usd": 1000.0,
        "stake": 25.0,
        "ts": "2026-05-08T11:55:00+00:00",
    }}
    out = render_today_settlements_block(settlements=settlements, placed_lookup=placed)
    assert "Djokovic" in out
    assert "WIN" in out
    assert "$+12.38" in out
    # Kelly P&L should be roughly 76.40*0.4953 = 37.83
    assert "$+37." in out
    # half-K = 152.81 * 0.4953 = 75.66
    assert "$+75." in out
```

- [ ] **Step 2: Run, verify fail**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```

- [ ] **Step 3: Implement**

Append to `tennis_portfolio.py`:
```python
def render_today_placer_activity_block(
    placed_today: list[dict],
    placer_skips: list[dict],
    replay: dict,
) -> str:
    """Render today's placer-fire log (placed + skipped) with per-mode stakes."""
    if not placed_today and not placer_skips:
        return "## Today's Placer Activity\n\n_No placer activity today._\n"

    from tennis_kelly import day_start_stake

    lines = [
        "## Today's Placer Activity",
        "",
        "| Pick | Opponent | SX Bet @T-15 | Base | ¼K | ½K | Edge | Result |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for p in placed_today:
        prob = float(p["model_prob"])
        odds = float(p["sxbet_odds"])
        avail = float(p.get("sxbet_available_usd", 0.0))
        edge = float(p.get("edge", 0.0))

        b = day_start_stake(
            mode="base", base_stake=25.0, kelly_multiplier=0.0,
            day_start_balance=replay["base"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )
        q = day_start_stake(
            mode="quarter_kelly", base_stake=25.0, kelly_multiplier=0.25,
            day_start_balance=replay["quarter_kelly"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )
        h = day_start_stake(
            mode="half_kelly", base_stake=25.0, kelly_multiplier=0.5,
            day_start_balance=replay["half_kelly"]["today_start_balance"],
            prob=prob, decimal_odds=odds, liquidity_usd=avail,
        )

        lines.append(
            f"| {p.get('pick','?')} | {p.get('opponent','?')} | "
            f"{odds:.3f} | {_money_abs(b['stake'])} | "
            f"{_money_abs(q['stake'])} | {_money_abs(h['stake'])} | "
            f"{_pct(edge*100)} | placed ({p.get('mode','dry_run')}) |"
        )

    for s in placer_skips:
        odds = s.get("sxbet_odds")
        odds_str = f"{float(odds):.3f}" if isinstance(odds, (int, float)) else "—"
        edge = s.get("edge")
        edge_str = _pct(edge * 100) if isinstance(edge, (int, float)) else "—"

        lines.append(
            f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
            f"{odds_str} | — | — | — | {edge_str} | "
            f"skipped: {s.get('reason','?')} |"
        )

    lines.append("")
    return "\n".join(lines)


def render_today_settlements_block(
    settlements: list[dict],
    placed_lookup: dict,
) -> str:
    """Render today's settled trades with per-mode P&L."""
    if not settlements:
        return "## Today's Settlements\n\n_No settlements today._\n"

    from tennis_kelly import kelly_fraction

    lines = [
        "## Today's Settlements",
        "",
        "| Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for s in settlements:
        pid = s["pick_id"]
        p = placed_lookup.get(pid, {})
        odds = float(p.get("sxbet_odds", 0.0))
        prob = float(p.get("model_prob", 0.0))
        avail = float(p.get("sxbet_available_usd", 0.0))
        won = bool(s.get("won", False))
        outcome = "WIN" if won else "LOSS"

        # base
        b_stake = min(25.0, avail) if avail else 25.0
        b_pnl = b_stake * (odds - 1.0) if won else -b_stake

        # Kelly modes use day-start = $500 approximation; canonical
        # numbers come via Performance block from the replay.
        f = kelly_fraction(prob=prob, decimal_odds=odds)
        q_stake = min(0.25 * f * 500.0, avail) if (f > 0 and avail) else 0.0
        h_stake = min(0.5 * f * 500.0, avail) if (f > 0 and avail) else 0.0
        q_pnl = q_stake * (odds - 1.0) if won else -q_stake
        h_pnl = h_stake * (odds - 1.0) if won else -h_stake

        lines.append(
            f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
            f"{odds:.3f} | {outcome} | "
            f"{_money(b_pnl)} | {_money(q_pnl)} | {_money(h_pnl)} |"
        )
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
python -m pytest tests/test_tennis_portfolio.py -p no:anchorpy -v
```
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add tennis_dry_run/tennis_portfolio.py tennis_dry_run/tests/test_tennis_portfolio.py
git commit -m "$(cat <<'EOF'
feat(tennis_portfolio): today's placer activity and settlements blocks

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Wire identifier — daily file BOD + rolling file refresh

Replace the existing `write_vault_report` function with calls to the portfolio renderer. Also write/overwrite the rolling `Tennis-Dry-Run-Report.md` file with the current snapshot.

**Files:**
- Modify: `tennis_dry_run/tennis_identifier.py:197-244` (replace `write_vault_report`)
- Modify: `tennis_dry_run/tennis_identifier.py:391-399` (call into new writer)
- Modify: `tennis_dry_run/tests/test_tennis_identifier.py` (extend coverage)

- [ ] **Step 1: Read existing identifier vault writer to understand structure**

```bash
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas"
sed -n '195,260p' tennis_dry_run/tennis_identifier.py
sed -n '390,400p' tennis_dry_run/tennis_identifier.py
```
Expected: see the existing `write_vault_report(now_utc, counts, selections, ...)` function returning a Path; main calls it after building `selections_for_report`.

- [ ] **Step 2: Add failing test**

Add to `tennis_dry_run/tests/test_tennis_identifier.py` (find existing test for `write_vault_report` and add alongside):
```python
def test_identifier_writes_portfolio_block_to_daily_file(tmp_path):
    """The new BOD section must include the Portfolio block + Identified Picks."""
    from tennis_identifier import write_daily_report

    selections = [{
        "pick": "Novak Djokovic", "opponent": "Dino Prizmic",
        "league": "ATP Rome", "surface": "clay",
        "model_prob": 0.8713, "fair_odds": 1.148,
        "sxbet_odds": 1.5534, "sxbet_available_usd": 39.05,
        "edge": 0.226,
        "game_time_iso": "2026-05-08T12:10:00+00:00",
        "placement_path": "scheduled",
        "scheduled_at_iso": "2026-05-08T11:55:00+00:00",
    }]
    counts = {"qualified": 1, "scheduled": 1, "immediate": 0,
              "skipped_dedup": 0, "skipped_filter": 0}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"balance": 500.0, "open_picks": {}}')
    (state_dir / "trades.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    rolling_path = tmp_path / "rolling.md"

    from datetime import datetime, timezone
    out = write_daily_report(
        now_utc=datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        counts=counts,
        selections=selections,
        markets_total=73, markets_today=47,
        vault_dir=vault_dir,
        state_dir=state_dir,
        rolling_path=rolling_path,
    )

    body = out.read_text(encoding="utf-8")
    assert "### Portfolio (snapshot 2026-05-08 07:00 UTC)" in body
    assert "Novak Djokovic" in body
    assert "1.553" in body
    assert "$39.05" in body
    # Rolling file written
    rolling = rolling_path.read_text(encoding="utf-8")
    assert "## Tennis Dry Run Report" in rolling
    assert "### Portfolio" in rolling
```

- [ ] **Step 3: Run, verify fail (function not defined)**

```bash
cd tennis_dry_run
python -m pytest tests/test_tennis_identifier.py::test_identifier_writes_portfolio_block_to_daily_file -p no:anchorpy -v
```
Expected: ImportError on `write_daily_report`.

- [ ] **Step 4: Implement — replace `write_vault_report` with `write_daily_report`**

In `tennis_dry_run/tennis_identifier.py`, replace lines 197-244 (the existing `write_vault_report` function) with:

```python
def write_daily_report(
    now_utc: datetime,
    counts: dict,
    selections: list[dict],
    markets_total: int,
    markets_today: int,
    vault_dir: Path,
    state_dir: Path,
    rolling_path: Path | None = None,
) -> Path:
    """Write daily BOD report + refresh rolling file.

    Daily file: <vault_dir>/YYYY-MM-DD.md — Portfolio + Identified Picks.
    Rolling file (optional): single file rewritten with current snapshot.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tennis_kelly import replay_three_bankrolls
    from tennis_portfolio import (
        render_portfolio_block,
        render_open_picks_block,
        render_closed_trades_block,
        render_performance_block,
        render_backtest_comparison_block,
        render_identified_picks_block,
    )
    import json

    vault_dir = Path(vault_dir)
    vault_dir.mkdir(parents=True, exist_ok=True)
    date_str = now_utc.date().isoformat()
    out_path = vault_dir / f"{date_str}.md"

    # Load state + trades
    state_file = state_dir / "state.json"
    trades_file = state_dir / "trades.jsonl"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    placed = []
    settled = []
    if trades_file.exists():
        for line in trades_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("type") == "open":
                placed.append(row)
            elif row.get("type") == "settled":
                settled.append(row)

    replay = replay_three_bankrolls(settled, placed, starting_balance=500.0,
                                    today=now_utc.date())

    # Daily file
    lines = [
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
        render_portfolio_block(replay, now_utc),
        "## Scan Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Markets total | {markets_total} |",
        f"| Markets today | {markets_today} |",
        f"| Qualified | {counts.get('qualified', 0)} |",
        f"| Scheduled (`at`) | {counts.get('scheduled', 0)} |",
        f"| Placed immediately | {counts.get('immediate', 0)} |",
        f"| Skipped (dedup) | {counts.get('skipped_dedup', 0)} |",
        f"| Skipped (filter) | {counts.get('skipped_filter', 0)} |",
        "",
        render_identified_picks_block(selections),
        "---",
        f"_Generated by `tennis_identifier.py` on the LXII Capital VPS._",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Rolling file (full re-render)
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
            f"_Generated by `tennis_identifier.py` at "
            f"{now_utc.strftime('%H:%M')} UTC._",
            "",
        ]
        rolling_path.parent.mkdir(parents=True, exist_ok=True)
        rolling_path.write_text("\n".join(rolling_lines), encoding="utf-8")

    return out_path
```

Then update lines 391-399 (the `main()` call site that invokes `write_vault_report`):
```python
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
            )
            log.info("Daily report written: %s", report_path)
            if rolling_path:
                log.info("Rolling report refreshed: %s", rolling_path)
        except Exception as exc:
            log.warning("Vault report write failed: %s", exc)
```

- [ ] **Step 5: Run, verify pass**

```bash
python -m pytest tests/test_tennis_identifier.py -p no:anchorpy -v
```
Expected: all existing tests still pass + new test passes.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -p no:anchorpy
```
Expected: 79 passed (63 existing + 16 portfolio + new identifier test, minus any deleted obsolete tests). If old tests reference `write_vault_report`, update them to `write_daily_report` or delete obsolete assertions.

- [ ] **Step 7: Commit**

```bash
git add tennis_dry_run/tennis_identifier.py tennis_dry_run/tests/test_tennis_identifier.py
git commit -m "$(cat <<'EOF'
feat(tennis_identifier): write daily report + refresh rolling file

Replace write_vault_report with write_daily_report which calls into
tennis_portfolio renderers. Adds Portfolio block to daily file BOD
section and writes a rolling Tennis-Dry-Run-Report.md with the
current snapshot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Wire eod_report — daily EOD section + rolling file re-render

Replace `render_eod_section` with a writer that uses the portfolio renderer for Today's Placer Activity + Today's Settlements + Portfolio refresh, then re-renders the rolling file.

**Files:**
- Modify: `tennis_dry_run/tennis_eod_report.py:65-174` (replace `aggregate_eod` + `render_eod_section`)
- Modify: `tennis_dry_run/tennis_eod_report.py:210-245` (update main flow)
- Modify: `tennis_dry_run/tests/test_tennis_eod_report.py`

- [ ] **Step 1: Read existing EOD code**

```bash
sed -n '60,175p' tennis_dry_run/tennis_eod_report.py
sed -n '200,250p' tennis_dry_run/tennis_eod_report.py
```

- [ ] **Step 2: Add failing test**

Add to `test_tennis_eod_report.py`:
```python
def test_eod_writes_portfolio_and_kelly_pnl_columns(tmp_path):
    """EOD section must include Portfolio block + Today's Placer Activity + Today's Settlements with Kelly columns."""
    from datetime import datetime, timezone
    from tennis_eod_report import write_eod_report

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        '{"balance": 500.0, "open_picks": {}, "today_bets": 0, "today_date": "2026-05-08"}'
    )
    (state_dir / "trades.jsonl").write_text(
        '{"type":"open","pick_id":"0xdjk","pick":"Novak Djokovic","opponent":"Dino Prizmic","league":"ATP Rome","surface":"clay","model_prob":0.8713,"sxbet_odds":1.4953,"sxbet_available_usd":1000.0,"stake":25.0,"ts":"2026-05-08T11:55:00+00:00","mode":"dry_run","edge":0.2026}\n'
        '{"type":"settled","pick_id":"0xdjk","pick":"Novak Djokovic","opponent":"Dino Prizmic","won":true,"pnl":12.38,"ts":"2026-05-08T15:00:00+00:00"}\n'
    )
    (state_dir / "skipped.jsonl").write_text(
        '{"type":"skipped","source":"placer","reason":"negative_edge","pick_id":"0xzv","pick":"Alexander Zverev","opponent":"Daniel Altmaier","league":"ATP Rome","sxbet_odds":1.087,"edge":-0.063,"ts":"2026-05-08T10:45:00+00:00"}\n'
    )
    (state_dir / "pending_selections.jsonl").write_text("")

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    daily_path = vault_dir / "2026-05-08.md"
    daily_path.write_text("# Tennis Daily Report — 2026-05-08\n\n_existing BOD content_\n")
    rolling_path = tmp_path / "rolling.md"

    out = write_eod_report(
        now_utc=datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc),
        state_dir=state_dir,
        vault_dir=vault_dir,
        rolling_path=rolling_path,
    )

    body = out.read_text(encoding="utf-8")
    # BOD content preserved
    assert "_existing BOD content_" in body
    # EOD section appended
    assert "## EOD Performance — 2026-05-08" in body
    assert "### Portfolio (snapshot 2026-05-08 22:00 UTC)" in body
    assert "## Today's Placer Activity" in body
    assert "Djokovic" in body
    assert "Zverev" in body
    assert "skipped: negative_edge" in body
    assert "## Today's Settlements" in body
    assert "WIN" in body
    assert "$+12.38" in body  # base
    assert "$+37." in body    # quarter-K
    assert "$+75." in body    # half-K
    # Rolling file re-rendered
    rolling = rolling_path.read_text(encoding="utf-8")
    assert "## Tennis Dry Run Report" in rolling
    assert "### Performance (cumulative)" in rolling
    assert "### Backtest vs Dry Run" in rolling
```

- [ ] **Step 3: Run, verify fail**

```bash
python -m pytest tests/test_tennis_eod_report.py::test_eod_writes_portfolio_and_kelly_pnl_columns -p no:anchorpy -v
```
Expected: ImportError on `write_eod_report`.

- [ ] **Step 4: Implement**

In `tennis_dry_run/tennis_eod_report.py`, replace `aggregate_eod` and `render_eod_section` (lines 65-174) and `main` (lines 210-245) with a new `write_eod_report` function and updated main:

```python
def write_eod_report(
    now_utc: datetime,
    state_dir: Path,
    vault_dir: Path,
    rolling_path: Path | None = None,
) -> Path:
    """Append/replace EOD section in today's daily file + re-render rolling file."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tennis_kelly import replay_three_bankrolls
    from tennis_portfolio import (
        render_portfolio_block,
        render_open_picks_block,
        render_closed_trades_block,
        render_performance_block,
        render_backtest_comparison_block,
        render_today_placer_activity_block,
        render_today_settlements_block,
    )

    today = now_utc.date()
    today_iso = today.isoformat()

    state_file = state_dir / "state.json"
    trades_file = state_dir / "trades.jsonl"
    skipped_file = state_dir / "skipped.jsonl"

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    all_trades = _read_jsonl(trades_file)
    skipped_today = [
        s for s in _read_jsonl(skipped_file)
        if _is_today(s.get("ts", ""), today)
    ]

    placed = [t for t in all_trades if t.get("type") == "open"]
    settled = [t for t in all_trades if t.get("type") == "settled"]
    placed_today = [p for p in placed if _is_today(p.get("ts", ""), today)]
    settled_today = [s for s in settled if _is_today(s.get("ts", ""), today)]
    placed_lookup = {p["pick_id"]: p for p in placed}
    placer_skips_today = [s for s in skipped_today if s.get("source") == "placer"]

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
        render_today_settlements_block(settled_today, placed_lookup),
        "---",
        f"_Generated by `tennis_eod_report.py` at "
        f"{now_utc.strftime('%H:%M')} UTC._",
        "",
    ])

    # Append/replace EOD section in daily file
    report_path = vault_dir / f"{today_iso}.md"
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
        rolling_path.parent.mkdir(parents=True, exist_ok=True)
        rolling_path.write_text("\n".join(rolling_lines), encoding="utf-8")

    return report_path


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    today = _today_utc()
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
```

If existing tests reference `aggregate_eod` or `render_eod_section`, update them to call `write_eod_report` directly or remove if redundant.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -p no:anchorpy
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tennis_dry_run/tennis_eod_report.py tennis_dry_run/tests/test_tennis_eod_report.py
git commit -m "$(cat <<'EOF'
feat(tennis_eod_report): write_eod_report with Portfolio + Kelly columns

Replace aggregate_eod + render_eod_section with a single
write_eod_report that uses tennis_portfolio renderers. Adds Today's
Placer Activity (placed + skipped, per-mode stakes) and Today's
Settlements (per-mode P&L) tables, and re-renders the rolling
Tennis-Dry-Run-Report.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Local smoke test against a copy of VPS state

Pull the current VPS `state.json` + `trades.jsonl` + `skipped.jsonl` to a temp dir locally, run both scripts in dry-mode against them, and eyeball the rendered markdown.

**Files:**
- No code changes; verification only.

- [ ] **Step 1: Pull VPS state files locally**

```bash
mkdir -p /tmp/tennis-smoke/.tmp /tmp/tennis-smoke/vault
scp vps:/opt/tennis-dry-run/.tmp/state.json /tmp/tennis-smoke/.tmp/
scp vps:/opt/tennis-dry-run/.tmp/trades.jsonl /tmp/tennis-smoke/.tmp/
scp vps:/opt/tennis-dry-run/.tmp/skipped.jsonl /tmp/tennis-smoke/.tmp/
scp vps:/opt/tennis-dry-run/.tmp/pending_selections.jsonl /tmp/tennis-smoke/.tmp/
ls -la /tmp/tennis-smoke/.tmp/
```
Expected: 4 files copied locally.

- [ ] **Step 2: Run identifier in dry mode (skip the actual scan; just write report from state)**

Identifier scans live SX Bet — for smoke we only want the report-writing path. Easier: invoke `write_daily_report` directly:

```bash
cd "c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
python -c "
from datetime import datetime, timezone
from pathlib import Path
from tennis_identifier import write_daily_report
out = write_daily_report(
    now_utc=datetime.now(timezone.utc),
    counts={'qualified':3,'scheduled':3,'immediate':0,'skipped_dedup':0,'skipped_filter':44},
    selections=[],  # skip selection rendering for smoke
    markets_total=73, markets_today=47,
    vault_dir=Path('/tmp/tennis-smoke/vault'),
    state_dir=Path('/tmp/tennis-smoke/.tmp'),
    rolling_path=Path('/tmp/tennis-smoke/Tennis-Dry-Run-Report.md'),
)
print('daily:', out)
"
cat /tmp/tennis-smoke/Tennis-Dry-Run-Report.md | head -60
```
Expected: rolling file shows Portfolio block with Djokovic open pick, Closed Trades section (empty until settlement lands), zero Performance.

- [ ] **Step 3: Run eod_report against the same state**

```bash
python -c "
from datetime import datetime, timezone
from pathlib import Path
from tennis_eod_report import write_eod_report
out = write_eod_report(
    now_utc=datetime.now(timezone.utc),
    state_dir=Path('/tmp/tennis-smoke/.tmp'),
    vault_dir=Path('/tmp/tennis-smoke/vault'),
    rolling_path=Path('/tmp/tennis-smoke/Tennis-Dry-Run-Report.md'),
)
print('daily:', out)
"
cat /tmp/tennis-smoke/Tennis-Dry-Run-Report.md
```
Expected: rolling file refreshed; daily file has both BOD (from step 2 stub) + EOD sections.

- [ ] **Step 4: Visual inspection checklist**

Confirm in the smoke output:
- [ ] Portfolio block has 11 rows × 3 columns; all numbers formatted with sign
- [ ] Open Picks shows Djokovic at 1.495 with $25 / $76+ / $152+ stakes
- [ ] Closed Trades shows `_No closed trades yet._` until Djokovic settles
- [ ] Performance shows `_No closed trades yet._` until Djokovic settles
- [ ] No raw Python repr (`{'`) leaking into the markdown
- [ ] No `KeyError` / `TypeError` exceptions in the run

If any issue found: fix in tennis_kelly.py or tennis_portfolio.py, re-run unit tests, re-run smoke.

- [ ] **Step 5: Commit any fixes**

If fixes were needed:
```bash
git add -p tennis_dry_run/
git commit -m "fix(tennis_portfolio): smoke-test fixups"
```

---

## Task 12: Deploy to VPS and verify next cron run

Push the new modules + modified scripts to VPS, restart the scan-loop daemon, wait for the next cron firing, verify the rolling file appears in vault.

**Files:**
- No code changes; deploy + verify only.

- [ ] **Step 1: Back up current VPS scripts**

```bash
ssh vps 'cd /opt/tennis-dry-run && cp tennis_identifier.py tennis_identifier.py.bak.$(date +%Y%m%d-%H%M%S) && cp tennis_eod_report.py tennis_eod_report.py.bak.$(date +%Y%m%d-%H%M%S)'
```

- [ ] **Step 2: scp new files**

```bash
LOCAL="c:/Users/ConorLowth/Desktop/Agentic Workflows/LXII Vegas/tennis_dry_run"
scp "$LOCAL/tennis_kelly.py"      vps:/opt/tennis-dry-run/
scp "$LOCAL/tennis_portfolio.py"  vps:/opt/tennis-dry-run/
scp "$LOCAL/tennis_identifier.py" vps:/opt/tennis-dry-run/
scp "$LOCAL/tennis_eod_report.py" vps:/opt/tennis-dry-run/
```

- [ ] **Step 3: Restart scan-loop daemon (picks up shared module changes)**

```bash
ssh vps 'systemctl restart tennis-dry-run && sleep 2 && systemctl is-active tennis-dry-run'
```
Expected: `active`

- [ ] **Step 4: Run a one-shot smoke against live VPS state**

```bash
ssh vps 'cd /opt/tennis-dry-run && ./venv/bin/python -c "
from datetime import datetime, timezone
from pathlib import Path
import os
from tennis_eod_report import write_eod_report
out = write_eod_report(
    now_utc=datetime.now(timezone.utc),
    state_dir=Path(\"/tmp/tennis-smoke-vps\"),  # mirror VPS .tmp first
    vault_dir=Path(\"/tmp/tennis-smoke-vps/vault\"),
    rolling_path=Path(\"/tmp/tennis-smoke-vps/Tennis-Dry-Run-Report.md\"),
)
print(out)
" 2>&1 || echo "(skipped — runs on next cron)"'
```
Or wait for next cron — see step 5.

- [ ] **Step 5: Wait for next cron firing and verify**

The 22:00 UTC EOD cron should fire next. After it runs:
```bash
ssh vps 'journalctl -t tennis-eod-report --since "1 hour ago" --no-pager | tail -20'
ssh vps 'ls -la /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Tennis-Dry-Run-Report.md'
ssh vps 'head -50 /opt/vps-hub/vault/finance-brain/10-Projects/Tennis-Automated/Tennis-Dry-Run-Report.md'
```
Expected: rolling file present with correct mtime and well-formed Portfolio block at the top.

- [ ] **Step 6: Final commit (deployment notes)**

If any small tweaks were made for VPS-side issues, commit them. Otherwise no commit needed for this task.

```bash
git status
git log --oneline | head -10
```
Expected: clean working tree; ~14 commits on the branch.

---

## Coverage check (self-review)

| Spec section | Implemented in task |
|---|---|
| 4. Architecture (two new modules) | Tasks 1-8 |
| 5.1 tennis_kelly.kelly_fraction | Task 1 |
| 5.1 tennis_kelly.day_start_stake | Task 2 |
| 5.1 tennis_kelly.replay_three_bankrolls | Task 3 |
| 5.2 portfolio render functions (8) | Tasks 4-8 |
| 5.3 identifier wiring | Task 9 |
| 5.4 eod_report wiring | Task 10 |
| 6. Daily file layout | Tasks 7, 8, 9, 10 |
| 7. Rolling file layout | Tasks 9, 10 |
| 8. Mechanics (Kelly, ROI, day boundaries) | Tasks 1-3 |
| 9. Edge cases | Tasks 1-3 (covered in unit tests) |
| 10. Testing | Each task includes its own tests |
| 11. Rollout (smoke + deploy) | Tasks 11-12 |
| 12. Out of scope | (intentionally excluded) |

All spec sections have an implementing task or are explicitly out-of-scope.
