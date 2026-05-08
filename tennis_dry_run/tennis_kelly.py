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
