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
    """Render currently-open picks table.

    Picks missing odds/prob render as flagged rows (header count reflects
    only fully-renderable picks), not silently dropped — silent drops were
    a reporting accuracy bug. Incomplete rows are visible at the bottom of
    the table with an `_(incomplete data)_` marker so they aren't lost.
    """
    if not open_picks:
        return "### Open Picks (0)\n\n_No open picks._\n"

    from tennis_kelly import day_start_stake

    renderable: list[tuple[str, dict]] = []
    incomplete: list[tuple[str, dict]] = []
    for pid, p in open_picks.items():
        odds = p.get("sxbet_odds")
        prob = p.get("model_prob")
        if (odds is None or prob is None
                or not isinstance(odds, (int, float)) or not isinstance(prob, (int, float))
                or float(odds) <= 1.0):
            incomplete.append((pid, p))
        else:
            renderable.append((pid, p))

    total = len(renderable)

    lines = [
        f"### Open Picks ({total})",
        "",
        "| Pick | Opponent | Match (UTC) | League | Entry odds | Edge | Base | ¼K | ½K |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for pid, p in renderable:
        odds = float(p["sxbet_odds"])
        prob = float(p["model_prob"])
        avail = float(p.get("sxbet_available_usd", 0.0))
        edge = float(p.get("edge", prob - 1.0 / odds))

        # Match (UTC) column: prefer game_time (unix seconds, set by placer when
        # the SX Bet selection includes it). Legacy state.json entries written
        # before game_time was threaded through render as "—" instead of the
        # placement timestamp (which would be misleading).
        gt = p.get("game_time")
        if isinstance(gt, (int, float)) and gt > 0:
            from datetime import datetime, timezone
            match_time = datetime.fromtimestamp(float(gt), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
        else:
            match_time = "—"

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

    for pid, p in incomplete:
        lines.append(
            f"| {p.get('pick','?')} _(incomplete data)_ | {p.get('opponent','?')} | — | "
            f"{p.get('league','?')} | — | — | — | — | — |"
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
        won = str(s.get("outcome", "")).lower() == "win"
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

    The identifier intentionally does not fetch the orderbook (late-binding
    to placer at T-15), so this block surfaces model + match metadata only.
    Scan-time SX Bet odds / edge / liquidity belong in the EOD placer-activity
    table, where the actual T-15 fetch is recorded.

    Args:
        selections: list of dicts with keys pick, opponent, league,
            surface, model_prob, fair_odds, game_time_iso,
            placement_path, scheduled_at_iso.
    """
    if not selections:
        return "## Identified Picks\n\n_No qualifying selections today._\n"

    lines = [
        "## Identified Picks",
        "",
        "| Pick | Opponent | League | Surface | Model Prob | Fair Odds | "
        "Match (UTC) | Placement |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for s in selections:
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
            f"{match_time} | {placement} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_shadow_picks_block(selections: list[dict]) -> str:
    """Render the 'Shadow Picks' table — tier-B selections (70-80% model
    confidence) that the identifier captured but did NOT schedule for
    placement.

    Optional columns appear when the selection dict carries:
      - `shadow_placement` (from tennis_shadow_placer at T-90): adds a
        "T-90 result" column (would_place / skip-reason) and "T-90 odds".
      - `status` ∈ {"WIN","LOSS","pending"} (post-resolution via
        tennis_eod_report.resolve_shadow_outcomes): adds Outcome + Theo PnL
        columns and an aggregate footer.
    """
    if not selections:
        return "## Shadow Picks (tier B, 70-80% — not placed)\n\n_No shadow (tier B) picks today._\n"

    has_outcomes = any("status" in s for s in selections)
    has_shadow_placement = any("shadow_placement" in s for s in selections)

    lines = [
        "## Shadow Picks (tier B, 70-80% — not placed)",
        "",
    ]

    headers = ["Pick", "Opponent", "League", "Surface", "Model Prob",
               "Fair Odds", "Match (UTC)"]
    aligns = ["", "", "", "", ":", ":", ""]
    if has_shadow_placement:
        headers += ["T-90 result", "T-90 odds"]
        aligns += ["", ":"]
    if has_outcomes:
        lines.append(
            "_Theoretical PnL = $25 stake × (fair_odds − 1) per win, −$25 per loss._"
        )
        lines.append("")
        headers += ["Outcome", "Theo PnL"]
        aligns += ["", ":"]

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(
        f"---{':' if a == ':' else ''}" for a in aligns
    ) + "|")

    for s in selections:
        match_time = (s.get("game_time_iso") or "")[:16].replace("T", " ")
        row = [
            s.get("pick", "?"),
            s.get("opponent", "?"),
            s.get("league", "?"),
            s.get("surface", "?"),
            f"{float(s.get('model_prob', 0)):.4f}",
            f"{float(s.get('fair_odds', 0)):.3f}",
            match_time,
        ]
        if has_shadow_placement:
            sp = s.get("shadow_placement") or {}
            sp_status = sp.get("status", "—")
            sp_reason = sp.get("reason")
            t90_label = sp_status if sp_status in ("would_place", "—") else (
                f"skip: {sp_reason}" if sp_reason else sp_status
            )
            sp_odds = sp.get("sxbet_odds")
            sp_odds_str = f"{float(sp_odds):.3f}" if sp_odds else "—"
            row += [t90_label, sp_odds_str]
        if has_outcomes:
            status = s.get("status", "pending")
            pnl = s.get("theoretical_pnl")
            pnl_str = "—" if status == "pending" or pnl is None else _money(float(pnl))
            row += [status, pnl_str]
        lines.append("| " + " | ".join(row) + " |")

    if has_outcomes:
        resolved = [s for s in selections if s.get("status") in ("WIN", "LOSS")]
        if resolved:
            wins = sum(1 for s in resolved if s["status"] == "WIN")
            total_pnl = sum(float(s.get("theoretical_pnl", 0)) for s in resolved)
            wr = wins / len(resolved) * 100
            lines += [
                "",
                f"**Resolved: {len(resolved)} | Wins: {wins} | "
                f"Win rate: {wr:.1f}% | Theoretical PnL: {_money(total_pnl)}**",
            ]
    if has_shadow_placement:
        with_sp = [s for s in selections if s.get("shadow_placement")]
        would_place = sum(
            1 for s in with_sp if s["shadow_placement"].get("status") == "would_place"
        )
        if with_sp:
            lines += [
                "",
                f"_T-90 placement evaluation: {would_place}/{len(with_sp)} would have placed._",
            ]

    lines.append("")
    return "\n".join(lines)


def render_stale_carryover_block(
    pending: list[dict],
    placer_skips_today: list[dict],
    settled_today: list[dict],
    now_utc: datetime,
) -> str:
    """Render picks whose match was today but never resulted in a settled trade.

    Surfaces what would otherwise vanish from the EOD report: picks that the
    placer skipped (negative_edge, odds_out_of_range, no_liquidity, ...) and
    picks where no placer attempt was logged at all.

    Args:
        pending: All entries from `pending_selections.jsonl` (may have dupes
            per pick_id; only one row per pick_id is rendered, latest wins).
        placer_skips_today: Skipped events from `skipped.jsonl` with
            `source == "placer"` whose `ts` is today (UTC).
        settled_today: Settled trade events from `trades.jsonl` whose `ts`
            is today (UTC).
        now_utc: Used to determine "today" UTC and to exclude future matches
            (placer hasn't fired yet).
    """
    today = now_utc.date()
    settled_ids = {s.get("pick_id") for s in settled_today if s.get("pick_id")}

    # Latest placer skip per pick_id (placer may skip multiple times).
    skip_by_pid: dict = {}
    for sk in placer_skips_today:
        pid = sk.get("pick_id")
        if not pid:
            continue
        prev = skip_by_pid.get(pid)
        if prev is None or sk.get("ts", "") > prev.get("ts", ""):
            skip_by_pid[pid] = sk

    # Dedup pending by pick_id (latest entry wins) and filter to today's
    # already-started matches that didn't settle.
    seen: set = set()
    rows: list[dict] = []
    for entry in reversed(pending):  # iterate newest-first if file is append-order
        pid = entry.get("pick_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        gt = entry.get("game_time")
        if gt is None:
            continue
        try:
            gt_dt = datetime.fromtimestamp(float(gt), tz=now_utc.tzinfo)
        except (TypeError, ValueError):
            continue
        if gt_dt.date() != today:
            continue
        if gt_dt > now_utc:
            continue  # match hasn't started — placer may still fire
        if pid in settled_ids:
            continue
        rows.append(entry)

    rows.reverse()  # restore chronological order for display

    if not rows:
        return "## Stale Carryovers\n\n_No stale carryovers._\n"

    lines = [
        "## Stale Carryovers",
        "",
        "_Today's picks that never settled — placer skip reason or no attempt logged._",
        "",
        "| Pick | Opponent | League | Match (UTC) | Reason | Last odds |",
        "|---|---|---|---|---|---:|",
    ]
    for r in rows:
        pid = r.get("pick_id")
        gt_dt = datetime.fromtimestamp(float(r["game_time"]), tz=now_utc.tzinfo)
        match_time = gt_dt.strftime("%Y-%m-%d %H:%M")
        sk = skip_by_pid.get(pid)
        if sk:
            reason = sk.get("reason", "?")
            odds = sk.get("sxbet_odds")
            odds_str = f"{float(odds):.3f}" if odds is not None else "—"
        else:
            reason = "no placer attempt logged"
            odds_str = "—"
        lines.append(
            f"| {r.get('pick','?')} | {r.get('opponent','?')} | "
            f"{r.get('league','?')} | {match_time} | {reason} | {odds_str} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_placer_rejection_diagnostics_block(
    placer_skips: list[dict],
    placed: list[dict],
    now_utc: datetime,
    window_days: int = 7,
) -> str:
    """Render N-day rolling distribution of placer outcomes.

    Each placer fire ends in either a `placed` trade or a `skipped` event with
    `source == "placer"`. This block aggregates them so it's obvious whether
    the strategy is bleeding signal to negative_edge / odds_out_of_range / etc.

    Args:
        placer_skips: Skipped events (typically from `skipped.jsonl`). Only
            entries with `source == "placer"` are counted; others (scan-time
            audit skips) are ignored.
        placed: Open-trade events (typically `[t for t in trades if type=='open']`).
        now_utc: Window endpoint.
        window_days: Window length in days (default 7).
    """
    from datetime import timedelta

    cutoff = now_utc - timedelta(days=window_days)

    def _in_window(ts_iso: str) -> bool:
        if not ts_iso:
            return False
        try:
            return datetime.fromisoformat(ts_iso).astimezone(now_utc.tzinfo) >= cutoff
        except Exception:
            return False

    counts: dict[str, int] = {}
    for sk in placer_skips:
        if sk.get("source") != "placer":
            continue
        if not _in_window(sk.get("ts", "")):
            continue
        reason = sk.get("reason", "?") or "?"
        counts[reason] = counts.get(reason, 0) + 1
    placed_in_window = sum(1 for p in placed if _in_window(p.get("ts", "")))
    if placed_in_window:
        counts["placed"] = placed_in_window

    total = sum(counts.values())
    if total == 0:
        return (
            f"## Placer Rejection Diagnostics (last {window_days} days)\n\n"
            f"_No placer attempts in window._\n"
        )

    # Sort: placed last, otherwise by descending count then alpha.
    def _sort_key(item):
        reason, n = item
        return (reason == "placed", -n, reason)

    lines = [
        f"## Placer Rejection Diagnostics (last {window_days} days)",
        "",
        "| Reason | Count | % of placer attempts |",
        "|---|---:|---:|",
    ]
    for reason, n in sorted(counts.items(), key=_sort_key):
        pct = n / total * 100
        lines.append(f"| {reason} | {n} | {pct:.0f}% |")
    lines.append(f"| **Total placer attempts** | **{total}** | 100% |")
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


def render_yesterday_recap_block(
    yesterday,
    settled_yesterday: list[dict],
    placed_lookup: dict,
    *,
    replay: dict | None = None,
) -> str:
    """Render a summary of yesterday's settlements at the top of today's BOD report.

    When `replay` is provided, ¼K/½K stakes use that sleeve's day_start_balance
    (canonical). When omitted, falls back to a starting-$500 approximation
    (legacy). Always prefer passing replay to avoid drift vs the Portfolio block.
    """
    from tennis_kelly import kelly_fraction

    iso = yesterday.isoformat()
    if not settled_yesterday:
        return f"## Yesterday's Results — {iso}\n\n_No settlements on {iso}._\n"

    qk_start = (replay or {}).get("quarter_kelly", {}).get("today_start_balance", 500.0)
    hk_start = (replay or {}).get("half_kelly", {}).get("today_start_balance", 500.0)

    lines = [
        f"## Yesterday's Results — {iso}",
        "",
        "| Pick | Opponent | Outcome | Base P&L | ¼K P&L | ½K P&L |",
        "|---|---|---|---:|---:|---:|",
    ]
    total_b = total_q = total_h = 0.0
    wins = losses = 0
    for s in settled_yesterday:
        # RETIRED outcomes have no P&L (stake voided). Render before the orphan
        # check so a retired orphan still gets a zero P&L row.
        if str(s.get("outcome", "")).lower() == "retired":
            lines.append(
                f"| {s.get('pick','?')} | {s.get('opponent','?')} | RETIRED | "
                f"$0.00 | $0.00 | $0.00 |"
            )
            continue

        p = placed_lookup.get(s.get("pick_id", ""))
        won = str(s.get("outcome", "")).lower() == "win"
        outcome = "WIN" if won else "LOSS"

        # Orphan: settlement with no matching parent `open` row. Render explicitly
        # so accuracy bugs (e.g. a winning settle that previously rendered as a
        # $25 LOSS via odds=0.0) become visible instead of silent.
        if p is None:
            lines.append(
                f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
                f"{outcome} _(orphan)_ | — | — | — |"
            )
            if won:
                wins += 1
            else:
                losses += 1
            continue

        odds = float(p.get("sxbet_odds", 0.0))
        prob = float(p.get("model_prob", 0.0))
        avail = float(p.get("sxbet_available_usd", 0.0))

        b_stake = min(25.0, avail) if avail else 25.0
        f = kelly_fraction(prob=prob, decimal_odds=odds)
        q_stake = min(0.25 * f * qk_start, avail) if (f > 0 and avail) else 0.0
        h_stake = min(0.5 * f * hk_start, avail) if (f > 0 and avail) else 0.0

        b_pnl = b_stake * (odds - 1.0) if won else -b_stake
        q_pnl = q_stake * (odds - 1.0) if won else -q_stake
        h_pnl = h_stake * (odds - 1.0) if won else -h_stake

        total_b += b_pnl
        total_q += q_pnl
        total_h += h_pnl
        if won:
            wins += 1
        else:
            losses += 1

        lines.append(
            f"| {s.get('pick','?')} | {s.get('opponent','?')} | {outcome} | "
            f"{_money(b_pnl)} | {_money(q_pnl)} | {_money(h_pnl)} |"
        )

    lines.append("")
    lines.append(
        f"**Day P&L:** {_money(total_b)} (base) · {_money(total_q)} (¼K) · "
        f"{_money(total_h)} (½K) · {wins} W / {losses} L"
    )
    lines.append("")
    return "\n".join(lines)


def render_today_settlements_block(
    settlements: list[dict],
    placed_lookup: dict,
    *,
    replay: dict | None = None,
) -> str:
    """Render today's settled trades with per-mode P&L.

    When `replay` is provided, ¼K/½K stakes use that sleeve's today_start_balance
    (canonical). When omitted, falls back to a starting-$500 approximation.
    """
    if not settlements:
        return "## Today's Settlements\n\n_No settlements today._\n"

    from tennis_kelly import kelly_fraction

    qk_start = (replay or {}).get("quarter_kelly", {}).get("today_start_balance", 500.0)
    hk_start = (replay or {}).get("half_kelly", {}).get("today_start_balance", 500.0)

    lines = [
        "## Today's Settlements",
        "",
        "| Pick | Opponent | Entry odds | Outcome | Base P&L | ¼K P&L | ½K P&L |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for s in settlements:
        # RETIRED outcomes have no P&L. Render before the orphan check so a
        # retired orphan still gets a zero P&L row.
        if str(s.get("outcome", "")).lower() == "retired":
            lines.append(
                f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
                f"— | RETIRED | $0.00 | $0.00 | $0.00 |"
            )
            continue

        pid = s["pick_id"]
        p = placed_lookup.get(pid)
        won = str(s.get("outcome", "")).lower() == "win"
        outcome = "WIN" if won else "LOSS"

        # Orphan: settlement with no matching parent `open` row. Render explicitly
        # so accuracy bugs (e.g. winning settle previously rendering as $-25 LOSS
        # via odds=0.0) become visible instead of silent.
        if p is None:
            lines.append(
                f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
                f"— | {outcome} _(orphan)_ | — | — | — |"
            )
            continue

        odds = float(p.get("sxbet_odds", 0.0))
        prob = float(p.get("model_prob", 0.0))
        avail = float(p.get("sxbet_available_usd", 0.0))

        b_stake = min(25.0, avail) if avail else 25.0
        b_pnl = b_stake * (odds - 1.0) if won else -b_stake

        f = kelly_fraction(prob=prob, decimal_odds=odds)
        q_stake = min(0.25 * f * qk_start, avail) if (f > 0 and avail) else 0.0
        h_stake = min(0.5 * f * hk_start, avail) if (f > 0 and avail) else 0.0
        q_pnl = q_stake * (odds - 1.0) if won else -q_stake
        h_pnl = h_stake * (odds - 1.0) if won else -h_stake

        lines.append(
            f"| {s.get('pick','?')} | {s.get('opponent','?')} | "
            f"{odds:.3f} | {outcome} | "
            f"{_money(b_pnl)} | {_money(q_pnl)} | {_money(h_pnl)} |"
        )
    lines.append("")
    return "\n".join(lines)
