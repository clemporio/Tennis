"""Kelly position sizing and three-bankroll replay for the tennis dry-run.

Pure functions — no I/O, no logging. Used by both tennis_identifier.py
and tennis_eod_report.py to compute Base / quarter-Kelly / half-Kelly
bankroll trajectories from the same trade journal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, date, timezone


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


_MODES = (
    ("base", 0.0),
    ("quarter_kelly", 0.25),
    ("half_kelly", 0.5),
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


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
        "open_stakes": {},                 # internal: pick_id -> stake_info
        "daily_pnl": defaultdict(float),   # internal: date -> pnl
        "daily_start_balance": {},         # internal: date -> balance at day start
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
    drawdown on settlement. Wins/losses come from settled rows' `won`
    flag; per-mode pnl is recomputed (the journal's `pnl` is base-stake-only).

    Args:
        settled_trades: rows from trades.jsonl with type == "settled"
        placed_trades:  rows from trades.jsonl with type == "open"
        starting_balance: Day-0 balance for every mode (default $500).
        today: UTC date used for today_pnl / today_roi computation.
            Defaults to current UTC date.

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

    # Build unified event timeline: (ts, kind, row).
    events: list[tuple[datetime, str, dict]] = []
    for r in placed_trades:
        events.append((_parse_ts(r["ts"]), "placed", r))
    for r in settled_trades:
        events.append((_parse_ts(r["ts"]), "settled", r))
    events.sort(key=lambda x: x[0])

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
                stake_info["entry_odds"] = float(row["sxbet_odds"])
                ms["open_stakes"][row["pick_id"]] = stake_info
                ms["deployed"] += stake_info["stake"]
                if stake_info["capped"]:
                    ms["capped_count"] += 1

            else:  # settled
                pick_id = row["pick_id"]
                stake_info = ms["open_stakes"].pop(pick_id, None)
                if stake_info is None:
                    continue  # orphaned settle row

                stake = stake_info["stake"]
                won = str(row.get("outcome", "")).lower() == "win"

                odds = stake_info["entry_odds"]
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
