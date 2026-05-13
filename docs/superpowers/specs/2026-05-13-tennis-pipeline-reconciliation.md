---
title: "Tennis Pipeline Reconciliation"
date: 2026-05-13
status: Proposed
authors: Plan Task 12 spinoff
---

# Spec: Tennis Pipeline Reconciliation

**Status:** Proposed. No implementation until reviewed.

**Date:** 2026-05-13

**Authors:** Plan Task 12 (Tennis Reporting Accuracy Remediation) spinoff.

## Problem

Two distinct pipelines currently create entries in `state.open_picks`:

1. **`tennis_dry_run.run_scan`** — long-running loop inside the `tennis-dry-run` service. The loop ticks every 60s (`tennis_dry_run.py:1414`), but `run_scan` itself only fires when `now_utc.hour ∈ SCAN_TIMES_UTC = [7, 14]` and that hour hasn't been scanned today (`tennis_dry_run.py:53`, `1392-1396`). On each fire it scans SX Bet markets, runs the predictor, applies filters, hits the orderbook, and on a clean pass writes directly to `state.open_picks[market_hash]` plus a journal `open` row.
2. **`tennis_identifier.evaluate_market` + `tennis_placer.place_pick`** — cron-spawned. The identifier runs once (cron, typically ~07:00 UTC). For each qualifying selection it persists a row to `pending_selections.jsonl` and either invokes the placer immediately (match within `PLACEMENT_LEAD_MIN`, default 15) or schedules an `at` job at `gameTime - lead_min` (`tennis_identifier.py:192-224`). The placer re-fetches the orderbook at T-15, re-applies the gates, calls `executor.place_order`, and under flock writes `state.open_picks[pick_id]` + a journal `open` row (`tennis_placer.py:140-156`).

The BOD daily report's "Qualified" count and "Identified Picks" section reflect ONLY the identifier's view (`tennis_identifier.py:601-617`, `write_daily_report`). Picks that `run_scan` opens directly (e.g., 2026-05-13 Gauff) appear under "Open Picks" but are invisible to the headline `qualified` count. Task 8 already added a clamped "Bot-opened (not counted in Qualified)" annotation derived as `max(0, len(open_picks) - counts['qualified'])` (`tennis_identifier.py:372`, `tennis_identifier.py:396`); the underlying duplication is unaddressed.

Beyond the reporting drift, the two pipelines have meaningfully different logic. See "Differences observed in code" below.

## Data flow today

| Process | Trigger | Inputs | Writes | Visible in BOD | Visible in EOD |
|---|---|---|---|---|---|
| `run_scan` (`tennis-dry-run` service) | Loop ticks every 60s; fires when `hour ∈ {7, 14} UTC` and hour not yet scanned today | SX Bet match-winner markets + TennisExplorer (for round map only) | `state.open_picks[market_hash]`, `trades.jsonl` (`type=open`), `skipped.jsonl`, `settlements.jsonl` (on settle) | "Open Picks" + the "Bot-opened (not counted)" annotation line (Task 8) | "Portfolio", "Today's Settlements" if settled |
| `evaluate_market` + `place_pick` | Cron daily (~07:00 UTC) for identifier; `at`-spawned at `gameTime - 15min` (or immediate) for placer | SX Bet markets + TennisExplorer (round map) for identifier; SX Bet orderbook re-fetched for placer | Identifier: `pending_selections.jsonl`, `shadow_selections.jsonl`, daily report `.md`, rolling report `.md`. Placer: `state.open_picks[pick_id]` (flocked), `trades.jsonl`, `skipped.jsonl` | "Qualified", "Identified Picks", "Shadow Picks", "Open Picks" | "Portfolio", "Today's Settlements" |

## Differences observed in code

These are real divergences between the two pipelines, not template defaults:

1. **Confidence threshold / tiering.** `run_scan` uses a single hard cutoff `MIN_CONFIDENCE = 0.80` (`tennis_dry_run.py:55`, `:1068`). The identifier uses `SHADOW_MIN_CONFIDENCE = 0.70` and splits selections into tier A (`prob >= 0.80`, scheduled for real placement) and tier B (`0.70 <= prob < 0.80`, "shadow" — never placed, written to a separate audit log via `tennis_shadow_placer.py`) (`tennis_identifier.py:45`, `:165-172`).
2. **League exclusions.** The identifier hard-rejects markets whose league name matches `EXCLUDED_LEAGUE_SUBSTRINGS = ("challenger", "qualifying", "qualif.", " q1", " q2", " q3", "itf ")` (`tennis_identifier.py:51-60`, `:130-131`). `run_scan` has no such filter — it relies only on the round filter and Elo presence, so a Challenger or Q-round market with a known-player Elo and recognised round can still be opened by the bot. This is a SEV-2 inconsistency relative to the "no Challenger / qualifying / ITF" memory rule (per `tennis_trading/feedback_no_challengers.md`).
3. **Daily cap.** `run_scan` enforces `MAX_DAILY_BETS = 10` (`tennis_dry_run.py:60`, `:1020-1022`). The identifier has no per-day cap; it places everything that clears tier A.
4. **Timing of orderbook fetch.** `run_scan` fetches the orderbook synchronously inside the scan at 07:00/14:00 UTC, often hours before kickoff — exactly the thin-book condition the late-binding redesign was meant to avoid. The placer fetches at T-15min when the book is live (`tennis_placer.py:95-96`). Picks opened by `run_scan` therefore have a known "thin-book outlier" risk surface that identifier+placer was specifically built to remove.
5. **Liquidity check.** Placer requires `available_usd >= PAPER_STAKE` (`tennis_placer.py:109-113`). `run_scan` accepts whatever the best price is and records `sxbet_available_usd` on the trade entry but does not gate on it.
6. **State write contention.** Both writers go through `update_state_atomic` / `save_state` (POSIX flock, `tennis_dry_run.py:277-320`). The placer uses a locked check-then-add (`tennis_placer.py:80-90`, `:147-151`); `run_scan` mutates `state` in memory and routes through `save_state(..., settled_ids=…)` which merges disk + bot views before the write (`tennis_dry_run.py:255-274`). Locking is correct but the merge means two pipelines can both open a pick on the SAME `market_hash` (the placer's pick_id is the market hash) if their windows overlap — last-writer's `trade_entry` wins but neither path detects the collision; both will be journalled as separate `open` rows in `trades.jsonl`. On Windows dev the lock is a no-op (`tennis_dry_run.py:31-34`).
7. **Today-only filter.** The identifier filters markets to `[now_utc, end_of_today_utc]` (`tennis_identifier.py:87-100`, `:521`). `run_scan` does not — it iterates every market `sxbet.get_all_tennis_markets()` returns, so it can open picks for matches starting tomorrow if the model agrees.
8. **Staking.** Both pipelines stake `config.base_stake_usd` (= `TENNIS_BASE_STAKE_USD`, default $25) — verified in `tennis_executor.py:52, 132, 179, 202, 326`. Neither pipeline currently applies a Kelly multiplier at order time. (Note: this contradicts the Task 12 template's "placer uses Kelly multiplier from selection metadata" — the codebase does not implement that. The Kelly-aware sizing is purely a *reporting* construct via `tennis_kelly.replay_three_bankrolls`, applied retrospectively in the report renderers, not a placer input.)
9. **Negative-edge check.** Both pipelines reject negative edge, but at different times: `run_scan` does it inline against the orderbook fetched at 07/14 UTC (`tennis_dry_run.py:1120-1127`); the placer does it at T-15 against the fresh orderbook (`tennis_placer.py:115-120`). Same logic, different prices.
10. **Settlement.** Only `run_scan`'s containing service runs `run_settle`. The placer does not settle; the identifier does not settle. So the bot service is *required* to be running for any pick (regardless of origin) to clear from `open_picks`. Retiring `run_scan` does not automatically remove settlement responsibility — that loop still has to live somewhere.
11. **Journal shape differences.** Both pipelines append `type=open` rows via `executor.place_order()` returning `result.trade_entry`. Shapes should be identical because both go through the same executor, but the `source` distinguishing the pipeline is not currently captured — there's no way to retroactively tell from `trades.jsonl` which pipeline opened a given pick.

## Options

### Option 1 — Retire `run_scan` (preferred)

Replace `run_scan` with identifier + placer in `mode=dry_run`. The placer already supports `dry_run` via the executor. The bot service becomes a thin "settle-only" loop (or `run_settle` is moved into the identifier cron and run on a tighter cadence, plus an explicit settle cron).

Pros:
- Single writer of `open_picks` (placer only). Eliminates the silent-drift class entirely.
- Reports reflect everything that's open (no "Bot-opened" annotation needed).
- Late-binding orderbook fetch becomes the only fetch path — the thin-book risk surface that motivated the identifier+placer redesign is fully eliminated.
- League-exclusion rule (no Challenger / qualifying / ITF) becomes universal — closes the SEV-2 gap (#2 above).
- One filter set to maintain (today-only, league-excluded, tier-tagged).

Cons:
- The 14:00 UTC scan goes away; only morning scans see new picks. Acceptable for the current sniper-style approach, but a real change in cadence.
- `run_scan`'s `MAX_DAILY_BETS = 10` cap disappears unless reimplemented in the identifier. The identifier currently has no daily cap.
- Loss of any picks `run_scan` would have caught between 07:00 and 14:00 UTC that the identifier missed at 07:00 (e.g., late-published markets). Needs a "second identifier pass" cron or accepting the gap.

### Option 2 — Unify reports, keep both pipelines

Keep both pipelines. Make the report consume `open_picks` regardless of origin and add a `source` column distinguishing them. The "Qualified" count becomes either (a) identifier-tier-A count + bot-opened count, or (b) two separate counts. Both should be visible.

Pros:
- No production behaviour change in the bot or identifier; lowest risk.
- Operator can audit exactly which pipeline opened which pick.

Cons:
- Two pipelines remain. Differences #1, #2, #3, #4, #7 above all persist — same selection logic divergence, just better reported.
- Future operators hit the same confusion class.
- The "no Challenger" rule is still violated by `run_scan` unless that filter is back-ported.

### Option 3 — Status quo + monitoring

Accept the inconsistency. Surface "bot vs identifier delta" as a line in the daily report (Task 8 already does this).

Pros:
- Zero code change to either pipeline.

Cons:
- Reporting accuracy debt remains.
- The `EXCLUDED_LEAGUE_SUBSTRINGS` divergence remains a hard violation of a stated trading rule.
- Two writers, two filter sets, two cadences — unbounded maintenance cost.

## Recommendation

**Option 1** (retire `run_scan`'s scanning role). It eliminates both the SEV-2 silent-drift class and the league-filter inconsistency in one move. Transition path:

1. **Audit pass.** Walk through the 11 differences above and confirm there are no others. Decide for each whether the identifier's behaviour or `run_scan`'s should be the canonical one going forward. Open items:
   - Reintroduce `MAX_DAILY_BETS` cap in the identifier? (Likely yes for safety.)
   - Reintroduce the 14:00 UTC scan as a second identifier cron? (Probably yes — catches markets published mid-day.)
   - Move `run_settle` into a separate cron, or keep the bot service running purely for settlement?
2. **Retain settlement.** Either keep the bot service alive in a settle-only configuration (call `run_settle` periodically, never `run_scan`), or split `run_settle` out into its own cron-driven script.
3. **Single deploy.** Comment out / gate off the `run_scan` call in `tennis_dry_run.main`, restart the bot service. Verify next BOD report shows `bot_opened = 0` and the "Bot-opened" annotation line is suppressed (or removed in the same PR).
4. **Backfill exclusion filter** if `run_scan` is kept around as a fallback path: apply `EXCLUDED_LEAGUE_SUBSTRINGS` symmetrically so the no-Challenger rule holds in either pipeline.

## Open questions

These are genuine gaps surfaced by reading the code, not template fillers:

- Should the identifier add `MAX_DAILY_BETS` once `run_scan` is retired? `run_scan` had it; the identifier doesn't.
- Should a second identifier cron run at 14:00 UTC to mirror the current `SCAN_TIMES_UTC = [7, 14]` cadence, or is 07:00 sufficient?
- Where does `run_settle` live after the retirement? Options: stays in the bot service in a settle-only mode; gets its own cron entry (e.g., every 30 min); moves into the identifier's main as a final step.
- Should `trade_entry` carry a `source: "run_scan" | "placer"` field so historical drift can be debugged from `trades.jsonl` alone? This is cheap to add immediately, regardless of which Option is chosen.
- The placer's `available_usd >= PAPER_STAKE` gate (`tennis_placer.py:109`) compares to a *flat* paper stake. Once Kelly sizing is integrated (separate work), this check needs to compare against the Kelly stake, not `PAPER_STAKE`. Note for future.
- Does the shadow track (tier B picks via `tennis_shadow_placer.py`) collide with anything if `run_scan` is retired? Shadow placer never writes to `state.open_picks`, so it's safe — confirm by re-reading `tennis_shadow_placer.py`.
- The `update_state_atomic` flock is a no-op on Windows (`tennis_dry_run.py:33`). Local dev does not exercise the race condition. Any reconciliation testing must happen on Linux (VPS or container) to be meaningful.

## Out of scope

- The placer's executor / signing logic (`tennis_executor.py`, `tennis_signing.py`).
- Reporting changes beyond the Task 8 "Bot-opened" annotation already shipped, and the removal of that line once Option 1 lands.
- Kelly-based stake sizing at order time (currently report-only via `tennis_kelly.replay_three_bankrolls`).
- Shadow-track refactoring.

## Next step

User reviews this spec and decides Option 1 / 2 / 3 before any code change. If Option 1, the audit pass (step 1 of the transition) becomes the next plan task.
