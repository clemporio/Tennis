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
