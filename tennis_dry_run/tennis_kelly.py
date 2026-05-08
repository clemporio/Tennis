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
