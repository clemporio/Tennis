---
date: 2026-05-08
status: approved
topic: tennis-reporting-flow
related:
  - tennis_dry_run/tennis_identifier.py
  - tennis_dry_run/tennis_eod_report.py
  - 10-Projects/Polymarket/Weather-Dry-Run-Report.md (reference pattern)
---

# Tennis Reporting Flow — Design

## 1. Background

Phase 2 dry-run is now placing real paper trades (first ever: Djokovic 2026-05-08 11:55 UTC, $25 @ 1.4953, +20.3% edge) following the SX Bet `marketHashes` param-name fix earlier the same day. Reporting needs to be upgraded to:

1. Surface the **real** SX Bet odds the bot now sees (previously corrupted by the param bug, now meaningful).
2. Track **cumulative running state** across days — balance, P&L, win rate, drawdown — flowing day into day, mirroring the Weather Dry Run reports.
3. Run **theoretical ¼-Kelly and ½-Kelly position-sizing tracks** alongside the base flat-$25 dry-run, using the same selections and odds taken, so we can compare bankroll trajectories under different risk regimes before committing to live execution.

## 2. Goals

- **Daily audit file** (`Daily-Reports/YYYY-MM-DD.md`) — per-day forensic record. BOD section written by `tennis_identifier.py` at 07:00 UTC; EOD section appended by `tennis_eod_report.py` at 22:00 UTC. Both refresh a Portfolio snapshot at the top.
- **Rolling cumulative file** (`Tennis-Dry-Run-Report.md`, single file) — at-a-glance dashboard refreshed by both crons. Mirrors the Weather Dry Run Report structure.
- **Three sizing tracks**: Base (flat $25), ¼-Kelly (computed), ½-Kelly (computed). All three share the same trade selections and the same `odds_taken` from real placer fires; only stake size differs.
- **Day-start-locked Kelly stakes**: Kelly stake for each mode is computed once per day from that mode's day-start balance, then frozen for every pick placed that day. Simpler reasoning than continuous intraday compounding.
- **Liquidity cap**: when computed Kelly stake exceeds SX Bet `available_usd` at placement time, cap the stake to liquidity and record the cap. Do not skip the pick.
- **Daily ROI semantics**: ROI = `today_pnl / day_start_balance`, not lifetime-from-origin. Cumulative tracking is via "Total P&L" (absolute $) and "Avg Daily ROI" (mean of daily ROIs).
- **TDD**: every module has unit tests. Kelly math, liquidity cap, day-start lock, and three-bankroll replay are deterministic on synthetic trade timelines. Rendering tests use golden fixtures.

## 3. Non-goals

- Changing the model, filter config, identifier scheduling, or executor live-mode logic.
- Backfilling historical days into the rolling file (start fresh from 2026-05-06 deploy).
- Real-time intraday balance compounding for Kelly (locked at day-start, see goals).
- Per-pick variant skipping when liquidity caps the stake — all three modes always place; smaller modes never cap before larger ones do (since stakes ascend Base ≤ ¼K ≤ ½K in normal cases).
- Visualization beyond markdown tables (no charts, no graphs).

## 4. Architecture

```text
+---------------------+      +---------------------+
| tennis_identifier   |      | tennis_eod_report   |
| (07:00 UTC cron)    |      | (22:00 UTC cron)    |
+----------+----------+      +----------+----------+
           |                            |
           |     +---------------------+|
           +---->| tennis_portfolio    |<+    pure functions:
                 |                     |      - Portfolio block
                 +-----+----------+----+      - Open Picks block
                       |          |
                       v          v
            +----------+----+ +---+----------+
            | tennis_kelly  | | rendering    |
            | (pure math)   | | helpers      |
            +---------------+ +--------------+

Sources of truth:
  state.json               -> open_picks, balance
  trades.jsonl             -> placed (type=open) and settled (type=settled) rows
  pending_selections.jsonl -> identifier output, by date
  skipped.jsonl            -> filter rejections (BOD context)
```

Two new modules; the two report scripts orchestrate them.

## 5. Module design

### 5.1 `tennis_kelly.py` (new, pure)

```python
def kelly_fraction(prob: float, decimal_odds: float) -> float
    # f* = (p*odds - 1) / (odds - 1); clamped to [0.0, 1.0]

def day_start_stake(
    *, mode: str,                # "base" | "quarter_kelly" | "half_kelly"
    base_stake: float,           # 25.0 for base
    kelly_multiplier: float,     # 0.0 (base), 0.25, 0.5
    day_start_balance: float,
    prob: float,
    decimal_odds: float,
    liquidity_usd: float,
) -> dict
    # returns {stake, pre_cap_stake, capped: bool}

def replay_three_bankrolls(
    settled_trades: list[dict],
    placed_trades: list[dict],
    starting_balance: float = 500.0,
) -> dict
    # walks the trade timeline in chronological order, returns:
    #   per-mode current balance, peak, drawdown, total_pnl,
    #   per-day P&L history, capped-trade count, daily-ROI list,
    #   open-pick stake totals (for "Deployed").
```

`replay_three_bankrolls` is the single source of truth for all derived metrics. Both report scripts call it with the same inputs.

### 5.2 `tennis_portfolio.py` (new, rendering)

```python
def render_portfolio_block(replay: dict, now_utc: datetime) -> str
def render_open_picks_block(open_picks: dict, replay: dict) -> str
def render_closed_trades_block(settled: list[dict], replay: dict, n: int = 30) -> str
def render_performance_block(replay: dict) -> str
def render_backtest_comparison_block(replay: dict) -> str
```

All return markdown strings. Pure given inputs.

### 5.3 `tennis_identifier.py` (modify)

After existing scan + scheduling logic:

1. Build replay over current trades + state.
2. Write/overwrite the daily file's BOD section: Portfolio block + `## Identified Picks` table (qualified-only, with real odds columns).
3. Refresh the rolling file: Portfolio + Open Picks blocks; leave Closed Trades / Performance / Backtest sections as written by last EOD run.

### 5.4 `tennis_eod_report.py` (modify)

1. Build replay over today's settled + placed trades + state.
2. Append/replace the daily file's EOD section: Today's Placer Activity, Today's Settlements.
3. Re-render the rolling file in full: Portfolio + Open Picks + Closed Trades + Performance + Backtest comparison.

## 6. Daily file layout (`Daily-Reports/YYYY-MM-DD.md`)

```markdown
---
date: YYYY-MM-DD
type: tennis-daily-report
tags: [tennis, dry-run]
---

# Tennis Daily Report — YYYY-MM-DD

### Portfolio (snapshot HH:MM UTC)
| Metric          | Base | ¼ Kelly | ½ Kelly |
|---|---:|---:|---:|
| Balance         | $X   | $X      | $X      |
| Starting        | $500 | $500    | $500    |
| Total P&L       | $±X  | $±X     | $±X     |
| Today P&L       | $±X  | $±X     | $±X     |
| Today ROI       | ±X%  | ±X%     | ±X%     |
| Avg Daily ROI   | ±X%  | ±X%     | ±X%     |
| Peak Balance    | $X   | $X      | $X      |
| Drawdown        | X%   | X%      | X%      |
| Deployed        | $X   | $X      | $X      |
| Today's Stake   | $25  | $X      | $X      |

## Identified Picks                              ← BOD, 07:00
| Pick | Opponent | League | Surface | Model Prob | Fair Odds | SX Bet @07:00 | Edge | Liquidity | Match (UTC) | Placement |

## Today's Placer Activity                       ← EOD, 22:00
| Pick | Opponent | SX Bet @T-15 | Base | ¼K | ½K | Edge | Result |

## Today's Settlements
| Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L |
```

Stake/P&L columns show actual cap-to-liquidity values; capped values get a trailing `*` and a footnote noting the pre-cap amount.

## 7. Rolling file layout (`Tennis-Dry-Run-Report.md`)

```markdown
---
tags: [tennis, dry-run, report]
type: report
---

## Tennis Dry Run Report — YYYY-MM-DD HH:MM UTC

### Portfolio
(same shape as daily, latest snapshot)

### Open Picks (N)
| Pick | Opponent | Match (UTC) | League | Entry odds | Edge | Base | ¼K | ½K |

### Closed Trades (last 30, newest first)
| Date | Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L | Result |

### Performance (cumulative)
| Metric            | Base | ¼ Kelly | ½ Kelly |
|---|---:|---:|---:|
| Total trades      | N    | N       | N       |
| Wins / Losses     | W/L  | W/L     | W/L     |
| Win rate          | X%   | X%      | X%      |
| Avg P&L/trade     | $X   | $X      | $X      |
| Profit factor     | X.XX | X.XX    | X.XX    |
| Largest win       | $X   | $X      | $X      |
| Largest loss      | $X   | $X      | $X      |
| Max drawdown      | X%   | X%      | X%      |
| Liquidity-capped  | n    | n       | n       |

### Backtest vs Dry Run
| Metric          | Backtest | Base | ¼ Kelly | ½ Kelly |
| Win rate        | 87.4%    | X%   | X%      | X%      |
| Profit factor   | 4.40     | X.XX | X.XX    | X.XX    |
| Sample size     | 11,161   | N    | N       | N       |
```

The rolling file always reflects the most recent run (07:00 BOD or 22:00 EOD); previous content is fully replaced.

## 8. Mechanics — Kelly, ROI, day boundaries

**Kelly fraction**: `f* = (p × odds - 1) / (odds - 1)`, clamped to `[0.0, 1.0]`. If `f* ≤ 0` (negative edge), stake is `0` for that pick in Kelly modes (placer would have skipped on `negative_edge` anyway, so this is defensive).

**Day-start stake (locked)**: at the first placer fire of the day for each mode, stake = `multiplier × f* × day_start_balance` for that mode's bankroll. The `day_start_balance` is the mode's balance as of `00:00 UTC` of that day. This stake value is reused for every pick that day in that mode.

**Liquidity cap**: at placement, if `stake > sxbet_available_usd`, set `stake = sxbet_available_usd`, mark `capped=True`, log pre-cap value. Pick still places.

**Daily ROI**: `today_pnl / day_start_balance × 100%`. The `day_start_balance` here is for the *whole portfolio* (per mode), not per-pick.

**Avg Daily ROI**: arithmetic mean of `daily_roi` over days where the mode has had at least one trade activity (pick fired or settlement landed). Days before the first trade ever placed in dry-run are excluded from the average.

**Day boundary**: UTC 00:00. P&L from a settlement at 23:55 UTC is "today's"; a settlement at 00:05 UTC the next day is the next day's, even if the trade was placed yesterday.

**Day-start balance carry-over**: a mode's `day_start_balance` for day D is its balance at the close of day D-1 (i.e. after all D-1 settlements have been applied). For day 0 (first day), `day_start_balance = $500` for every mode.

**Drawdown**: `(peak_balance - current_balance) / peak_balance × 100%`. Peak resets only when balance exceeds prior peak. Showed as `0%` when at new peak.

**Profit factor**: `sum(wins) / abs(sum(losses))`. If no losses, render as `∞`.

## 9. Edge cases

- **First-ever run / no settled trades**: Portfolio block shows zeros, Closed Trades shows `_No closed trades yet._`, Performance shows `n/a` for averages.
- **Day with zero placer fires**: Today P&L = $0, Today ROI = 0%, "Today's Stake" row shows the *would-have-been* stake had a pick fired (so we can verify Kelly math).
- **Settlement before placement on same day**: shouldn't happen (matches end after placement) but if it does, settle is processed in trade-timestamp order.
- **Capped stake makes ¼K stake equal to ½K stake**: render both with `*` footnote disambiguation.
- **Kelly `f*` > 1.0** (extreme edge): clamped to 1.0; ½K then = 50% of bankroll. Always cap further to liquidity.
- **Open pick deletion** (manual `state.json` edit): replay must tolerate orphaned settle rows whose `open` row has been removed; pair them by `pick_id` and ignore unmatchable.
- **Stale `pending_selections.jsonl`**: pick game_time may be wrong if TennisExplorer rescheduled. Report shows it as-recorded; doesn't break.
- **Rolling file race**: identifier and eod_report could in theory collide. They run 15 hours apart, so race is impossible in practice. No locking added.

## 10. Testing

`tennis_dry_run/tests/test_tennis_kelly.py`:

- `kelly_fraction` for known values (Djokovic example: 0.6112 ± 0.0001)
- Negative-edge clamps to 0
- `f* > 1.0` clamps to 1.0
- `day_start_stake` for each of 3 modes
- Liquidity cap triggers with `pre_cap_stake > liquidity_usd`
- `replay_three_bankrolls` deterministic on a 5-trade synthetic timeline (mix of wins/losses, one capped)

`tennis_dry_run/tests/test_tennis_portfolio.py`:

- Portfolio block render with zero trades
- Portfolio block render with mixed P&L (golden file)
- Open picks block with three open
- Closed trades block respects `n` limit
- Performance block with all-wins, all-losses, mixed
- Backtest comparison block

`tennis_dry_run/tests/test_tennis_identifier.py` (extend existing):

- Daily file BOD section includes Portfolio block + Identified Picks table with new columns
- Rolling file is refreshed with current Portfolio + Open Picks

`tennis_dry_run/tests/test_tennis_eod_report.py` (extend existing):

- EOD section includes Today's Placer Activity + Today's Settlements
- Rolling file fully re-rendered including Closed Trades + Performance + Backtest

Coverage target: same as existing modules (every public function tested + at least one golden render per block).

## 11. Rollout

1. Implement modules + tests on current branch (`tennis-phase2-executor`).
2. Run full pytest locally — must keep 63 → 63+N passing.
3. Smoke-test both reports locally against a copy of VPS state files.
4. `rsync` to VPS, restart `tennis-dry-run.service`. No cron edits required (existing 07:00 / 22:00 entries call the same scripts).
5. First production write: next 07:00 UTC identifier run. Verify rolling file appears in vault.
6. Verify EOD section the same day at 22:00 UTC.
7. Commit on the branch with paired test + impl commits per module.

## 12. Out of scope (parked)

- Centralized portfolio service across LTD / horse / tennis bots.
- Push notifications on Kelly drawdown thresholds.
- Frontend dashboard fed from `trades.jsonl`.
- The `today_bets` counter divergence between scan-loop and placer pipelines — separate cleanup task.
