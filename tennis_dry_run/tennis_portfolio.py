"""Markdown rendering for tennis dry-run reports.

Pure functions — given replay output and inputs, return strings.
"""

from __future__ import annotations

from datetime import datetime


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
    starting_balance: float = 500.0,
) -> str:
    """Render the Portfolio table — 3 columns (Base / 1/4 K / 1/2 K).

    Args:
        replay: output of tennis_kelly.replay_three_bankrolls
        now_utc: timestamp shown in the header
        base_stake_usd: flat base stake (shown in "Today's Stake" row)
        starting_balance: initial bankroll shown in the "Starting" row
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
        _row("Starting",      _money_abs(starting_balance),   _money_abs(starting_balance),   _money_abs(starting_balance)),
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
        odds_raw = p.get("sxbet_odds")
        if odds_raw is None:
            continue  # skip picks missing odds (legacy state.json entries)
        odds = float(odds_raw)
        prob_raw = p.get("model_prob")
        if prob_raw is None:
            continue  # skip picks missing model_prob (legacy state.json entries)
        prob = float(prob_raw)
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

        edge_sign = "+" if edge >= 0 else "-"
        lines.append(
            f"| {p.get('pick','?')} | {p.get('opponent','?')} | {match_time} | "
            f"{p.get('league','?')} | {odds:.3f} | {edge_sign}{abs(edge*100):.1f}% | "
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
    pull entry odds + stake. Per-mode P&L for display approximates Kelly
    stakes using starting balance ($500); canonical numbers come from
    the Performance block via the replay output.
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
        b_pnl = base_stake * (odds - 1.0) if won else -base_stake
        rows.append((ts, p, s, odds, won, b_pnl))

    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:n]

    from tennis_kelly import kelly_fraction

    lines = [
        f"### Closed Trades (last {min(n, len(rows))}, newest first)",
        "",
        "| Date | Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L | Result |",
        "|---|---|---|---:|---|---:|---:|---:|---|",
    ]
    for ts, p, s, odds, won, b_pnl in rows:
        result = "WIN" if won else "LOSS"
        outcome = "✓" if won else "✗"
        prob = float(p["model_prob"])
        f = kelly_fraction(prob=prob, decimal_odds=odds)
        avail = float(p.get("sxbet_available_usd", 0.0))
        qk_stake = min(0.25 * f * 500.0, avail) if (f > 0 and avail) else 0.0
        hk_stake = min(0.5 * f * 500.0, avail) if (f > 0 and avail) else 0.0
        qk_pnl = qk_stake * (odds - 1.0) if won else -qk_stake
        hk_pnl = hk_stake * (odds - 1.0) if won else -hk_stake

        lines.append(
            f"| {ts} | {p.get('pick','?')} | {p.get('opponent','?')} | "
            f"{odds:.3f} | {outcome} | "
            f"{_money(b_pnl)} | {_money(qk_pnl)} | {_money(hk_pnl)} | {result} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_performance_block(replay: dict) -> str:
    """Render cumulative performance table (Base / 1/4 K / 1/2 K)."""
    b, q, h = replay["base"], replay["quarter_kelly"], replay["half_kelly"]

    total = b["wins"] + b["losses"]
    if total == 0:
        return "### Performance (cumulative)\n\n_No closed trades yet._\n"

    win_rate = (b["wins"] / total) * 100.0

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
    walk-forward results: 87.4% SR, PF 4.40, 11,161 sampled matches at
    the 80%+ confidence + odds<=2.00 threshold.
    """
    b = replay["base"]
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

        b_stake = min(25.0, avail) if avail else 25.0
        b_pnl = b_stake * (odds - 1.0) if won else -b_stake

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
