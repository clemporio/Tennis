"""Tennis Model Dry Run — paper trading against live SX Bet markets.

Orchestrates the full loop:
  1. Scan TennisExplorer for high-confidence model picks.
  2. Find matching markets on SX Bet.
  3. Record paper trades (no real money placed).
  4. Settle open picks once results are available.
  5. Journal every trade and update running P&L.
"""

import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from tennis_executor import ExecutorConfig, TennisExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tennis_dry_run")

# ── Config ────────────────────────────────────────────────────────────────────

STARTING_BALANCE = 500.0

STATE_DIR = Path(__file__).resolve().parent / ".tmp"
STATE_FILE = STATE_DIR / "state.json"
JOURNAL_FILE = STATE_DIR / "trades.jsonl"
SKIPPED_FILE = STATE_DIR / "skipped.jsonl"
DAILY_FILE = STATE_DIR / "daily.jsonl"
ELO_FILE = STATE_DIR / "tennis_data" / "elo_ratings.json"

SCAN_TIMES_UTC = [7, 14]
SETTLE_INTERVAL = 3600
MIN_CONFIDENCE = 0.80
ROUNDS_FILTER = ["R128", "R64", "R32"]
MIN_ODDS = 1.01
MAX_ODDS = 2.00
PAPER_STAKE = float(os.getenv("TENNIS_BASE_STAKE_USD", "25.0"))
MAX_DAILY_BETS = 10

# ── Name Matching ─────────────────────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Strip accents (NFKD decomposition), lowercase, remove non-alpha except spaces.

    Args:
        name: Raw player name string.

    Returns:
        Normalized string: lowercase ASCII letters and spaces only.
    """
    # Decompose unicode characters, then drop combining marks
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase and strip everything except letters and spaces
    return re.sub(r"[^a-z ]", "", ascii_only.lower())


def _extract_last_and_initial(name: str) -> tuple[str, str]:
    """Extract (last_name, first_initial) from either TennisExplorer or SX Bet format.

    TennisExplorer format — last token is a single character (the initial):
        "Salkova D."     → ("salkova", "d")
        "Barrios Vera M."→ ("barrios vera", "m")

    SX Bet format — first token is the first name, rest is the last name:
        "Dominika Salkova"             → ("salkova", "d")
        "Marcelo Tomas Barrios Vera"   → ("tomas barrios vera", "m")

    Note: For SX Bet multi-word names the last-name extraction is imperfect.
    We validate *both* players match in find_sxbet_market, so false positives
    on one player are caught by requiring the other to match too.

    Args:
        name: Player name in either format (may contain accents).

    Returns:
        Tuple of (last_name, first_initial) — both lowercased, accent-free.
    """
    normalized = _normalize(name)
    tokens = normalized.split()

    if not tokens:
        return ("", "")

    # Detect TennisExplorer format: last token is a single letter (the initial).
    if len(tokens[-1]) == 1:
        initial = tokens[-1]
        last_name = " ".join(tokens[:-1])
        return (last_name, initial)

    # SX Bet format: first token is first name, everything else is last name.
    initial = tokens[0][0]
    last_name = " ".join(tokens[1:])
    return (last_name, initial)


def match_player_name(scanner_name: str, sxbet_name: str) -> bool:
    """Match a TennisExplorer player name to an SX Bet player name.

    Last names must match exactly. When both sides yield a single-letter
    initial, those initials must also match.

    Args:
        scanner_name: Player name from TennisExplorer (e.g. "Salkova D.").
        sxbet_name:   Player name from SX Bet (e.g. "Dominika Salkova").

    Returns:
        True if the names refer to the same player, False otherwise.
    """
    last_a, init_a = _extract_last_and_initial(scanner_name)
    last_b, init_b = _extract_last_and_initial(sxbet_name)

    if not last_a or not last_b:
        return False

    # Exact match is the common case.
    # For SX Bet multi-given-name players (e.g. "Marcelo Tomas Barrios Vera")
    # our SX Bet extraction yields "tomas barrios vera" while TennisExplorer
    # yields the true compound surname "barrios vera".  We accept a match when
    # one last name is a trailing suffix of the other (word-boundary aligned).
    if last_a != last_b:
        # Check whether one is a word-aligned suffix of the other
        longer, shorter = (last_a, last_b) if len(last_a) > len(last_b) else (last_b, last_a)
        if not (longer == shorter or longer.endswith(" " + shorter) or longer == shorter):
            return False

    # If both sides produced a usable initial, they must agree.
    if init_a and init_b and init_a != init_b:
        return False

    return True


def find_sxbet_market(
    pick_name: str,
    opponent_name: str,
    sxbet_markets: list[dict],
) -> Optional[dict]:
    """Find the SX Bet market where BOTH player names match (either order).

    Args:
        pick_name:      TennisExplorer name of the model's pick.
        opponent_name:  TennisExplorer name of the opponent.
        sxbet_markets:  List of normalized market dicts with keys:
                        market_hash, player_a, player_b, league, game_time, event_id.

    Returns:
        The matching market dict, or None if no match is found.
    """
    for market in sxbet_markets:
        player_a = market["player_a"]
        player_b = market["player_b"]

        # Both players must be present (in either order)
        if (
            match_player_name(pick_name, player_a)
            and match_player_name(opponent_name, player_b)
        ) or (
            match_player_name(pick_name, player_b)
            and match_player_name(opponent_name, player_a)
        ):
            return market

    return None


# ── State Management ──────────────────────────────────────────────────────────


def load_state(state_file: Path = STATE_FILE) -> dict:
    """Load bot state from disk, or return a fresh initial state.

    Args:
        state_file: Path to the JSON state file.

    Returns:
        State dict with keys: balance, total_bets, wins, losses, total_pnl,
        open_picks, today_bets, today_date, last_scan, last_settle.
    """
    if state_file.exists():
        try:
            with state_file.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load state from %s: %s — using fresh state", state_file, exc)

    return {
        "balance": STARTING_BALANCE,
        "total_bets": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0,
        "open_picks": {},
        "today_bets": 0,
        "today_date": None,
        "last_scan": None,
        "last_settle": None,
    }


def save_state(state: dict, state_file: Path = STATE_FILE) -> None:
    """Write bot state to disk as JSON.

    Creates parent directories if they do not exist.

    Args:
        state:      State dict to persist.
        state_file: Destination path for the JSON file.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def append_journal(entry: dict, journal_file: Path = JOURNAL_FILE) -> None:
    """Append one entry as a JSON line to a JSONL file.

    Creates parent directories if they do not exist.

    Args:
        entry:        Dict to serialize and append.
        journal_file: Destination JSONL file path.
    """
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    with journal_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── TennisExplorer Scraping ───────────────────────────────────────────────────

TENNIS_EXPLORER_URL = "https://www.tennisexplorer.com/matches/?type=all"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

SKIP_TOURNAMENTS = {"UTR Pro", "Futures", "ITF", "W15", "W25", "M15", "M25"}
CLAY_TOURNAMENTS = {"Bucharest", "Marrakech", "Barcelona", "Monte Carlo", "Madrid",
                    "Rome", "Roland Garros", "French Open", "Kitzbuhel", "Hamburg",
                    "Bastad", "Umag", "Gstaad", "Buenos Aires", "Rio", "Santiago",
                    "Cordoba", "Estoril", "Lyon", "Geneva", "Parma", "Bogota"}
GRASS_TOURNAMENTS = {"Wimbledon", "Queens", "Halle", "Stuttgart", "Eastbourne",
                     "s-Hertogenbosch", "Mallorca", "Newport"}


def _detect_surface(tournament: str) -> str:
    """Detect court surface from tournament name.

    Args:
        tournament: Full tournament name string.

    Returns:
        One of "hard", "clay", or "grass".
    """
    for city in CLAY_TOURNAMENTS:
        if city.lower() in tournament.lower():
            return "clay"
    for city in GRASS_TOURNAMENTS:
        if city.lower() in tournament.lower():
            return "grass"
    return "hard"


def _detect_level(tournament: str) -> str:
    """Detect tournament level from name.

    Args:
        tournament: Full tournament name string.

    Returns:
        One of "grand_slam", "masters", "challenger", or "main_tour".
    """
    t_lower = tournament.lower()
    grand_slams = {"australian open", "roland garros", "french open", "wimbledon", "us open"}
    for gs in grand_slams:
        if gs in t_lower:
            return "grand_slam"
    masters_keywords = {"masters", "1000", "atp 1000", "indian wells", "miami",
                        "monte carlo", "madrid", "rome", "canada", "cincinnati",
                        "shanghai", "paris", "beijing"}
    for kw in masters_keywords:
        if kw in t_lower:
            return "masters"
    if "challenger" in t_lower:
        return "challenger"
    return "main_tour"


def _detect_round(tournament: str) -> str:
    """Parse round from tournament name suffix like 'ATP Rome_R64' → 'R64'.

    Args:
        tournament: Tournament name potentially containing a round suffix.

    Returns:
        Round string (e.g. "R64") or "unknown" if not found.
    """
    match = re.search(r"_(R\d+|QF|SF|F|RR)$", tournament, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return "unknown"


def _should_skip(tournament: str) -> bool:
    """Check whether a tournament should be skipped due to low level.

    Args:
        tournament: Tournament name string.

    Returns:
        True if the tournament matches any entry in SKIP_TOURNAMENTS.
    """
    for skip_kw in SKIP_TOURNAMENTS:
        if skip_kw.lower() in tournament.lower():
            return True
    return False


def scrape_scheduled_matches() -> list[dict]:
    """Scrape upcoming scheduled matches from TennisExplorer.

    Parses HTML tables where rows with class 'head' are tournament headers and
    rows with class 'fRow' are the first player in a match (next row = second
    player).  Doubles matches and low-level tournaments are skipped.

    Returns:
        List of dicts with keys: player_a, player_b, tournament, time,
        surface, level, round.
    """
    try:
        resp = requests.get(TENNIS_EXPLORER_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to fetch TennisExplorer: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    matches: list[dict] = []
    current_tournament = ""

    rows = soup.select("table.result tr")
    i = 0
    while i < len(rows):
        row = rows[i]
        classes = row.get("class", [])

        # Tournament header row
        if "head" in classes:
            header_cell = row.find("td")
            if header_cell:
                current_tournament = header_cell.get_text(strip=True)
            i += 1
            continue

        # First-player row — TennisExplorer uses different class combos:
        #   "fRow" = first match in table, "odd"/"even" = alternating,
        #   "bott" = first player of a pair (most reliable indicator)
        if "bott" in classes or "fRow" in classes or "odd" in classes or "even" in classes:
            # Only process when it's a first-player row (has time cell)
            time_cell = row.find("td", class_="time")
            if time_cell is None:
                i += 1
                continue

            # Skip if tournament should be filtered out
            if _should_skip(current_tournament):
                i += 1
                continue

            # Get player A name
            player_cells = row.find_all("td", class_="t-name")
            if not player_cells:
                i += 1
                continue
            player_a_raw = player_cells[0].get_text(strip=True)

            # Skip doubles
            if " / " in player_a_raw:
                i += 1
                continue

            # Check score cells — if filled this is a completed match
            score_cells = row.find_all("td", class_="score")
            completed_sets = sum(
                1 for sc in score_cells if re.search(r"\d", sc.get_text())
            )
            if completed_sets >= 2:
                i += 1
                continue

            # Get player B from next row
            if i + 1 >= len(rows):
                i += 1
                continue
            next_row = rows[i + 1]
            next_player_cells = next_row.find_all("td", class_="t-name")
            if not next_player_cells:
                i += 1
                continue
            player_b_raw = next_player_cells[0].get_text(strip=True)

            # Skip doubles
            if " / " in player_b_raw:
                i += 2
                continue

            # Strip seedings like "(3)"
            player_a = re.sub(r"\(\d+\)", "", player_a_raw).strip()
            player_b = re.sub(r"\(\d+\)", "", player_b_raw).strip()

            match_time = time_cell.get_text(strip=True)

            matches.append({
                "player_a": player_a,
                "player_b": player_b,
                "tournament": current_tournament,
                "time": match_time,
                "surface": _detect_surface(current_tournament),
                "level": _detect_level(current_tournament),
                "round": _detect_round(current_tournament),
            })
            i += 2
            continue

        i += 1

    log.info("Scraped %d scheduled matches from TennisExplorer", len(matches))
    return matches


def _read_set_score(cell) -> Optional[int]:
    """Extract a player's games-won from a TennisExplorer set-score cell.

    A tiebreak set is rendered as `<td class="score">6<sup>10</sup></td>`;
    `get_text(strip=True)` would yield "610". We drop the `<sup>` so the
    returned value is the games count, not games concatenated with tiebreak
    points.
    """
    sup = cell.find("sup")
    if sup is not None:
        sup.extract()
    text = cell.get_text(strip=True)
    try:
        return int(text)
    except ValueError:
        return None


def scrape_completed_results(target_date=None) -> list[dict]:
    """Scrape completed match results from TennisExplorer.

    Looks for matches where score cells contain digits and at least 2 sets
    have been played.  Winner is determined by the player who won more sets
    (a set score >= 6 counts as a won set).

    Args:
        target_date: Optional `datetime.date` of the day to query. The default
            URL (`?type=all`) returns "today" in TennisExplorer's local
            timezone (Europe/Prague), which causes the page to roll over to
            tomorrow's empty schedule when called late in UTC. Passing an
            explicit date appends `&year=&month=&day=` and pins the view to
            that calendar day regardless of when we query.

    Returns:
        List of dicts with keys: player_a, player_b, winner, tournament.
    """
    if target_date is not None:
        url = (f"{TENNIS_EXPLORER_URL}&year={target_date.year}"
               f"&month={target_date.month}&day={target_date.day}")
    else:
        url = TENNIS_EXPLORER_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to fetch TennisExplorer results: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict] = []
    current_tournament = ""

    rows = soup.select("table.result tr")
    i = 0
    while i < len(rows):
        row = rows[i]
        classes = row.get("class", [])

        if "head" in classes:
            header_cell = row.find("td")
            if header_cell:
                current_tournament = header_cell.get_text(strip=True)
            i += 1
            continue

        time_cell = row.find("td", class_="time")
        if time_cell is None:
            i += 1
            continue

        player_cells = row.find_all("td", class_="t-name")
        if not player_cells:
            i += 1
            continue
        player_a_raw = player_cells[0].get_text(strip=True)
        if " / " in player_a_raw:
            i += 1
            continue

        # Check if this match is completed (2+ sets with scores)
        score_cells_a = row.find_all("td", class_="score")
        completed_sets_a = sum(
            1 for sc in score_cells_a if re.search(r"\d", sc.get_text())
        )
        if completed_sets_a < 2:
            i += 1
            continue

        if i + 1 >= len(rows):
            i += 1
            continue
        next_row = rows[i + 1]
        next_player_cells = next_row.find_all("td", class_="t-name")
        if not next_player_cells:
            i += 1
            continue
        player_b_raw = next_player_cells[0].get_text(strip=True)
        if " / " in player_b_raw:
            i += 2
            continue

        score_cells_b = next_row.find_all("td", class_="score")

        player_a = re.sub(r"\(\d+\)", "", player_a_raw).strip()
        player_b = re.sub(r"\(\d+\)", "", player_b_raw).strip()

        # Count sets won by each player.
        # TennisExplorer renders tiebreak set scores as e.g. "6<sup>10</sup>";
        # get_text would flatten that to "610". Strip <sup> so only the games
        # count (the 6) is parsed, not the tiebreak points.
        sets_a = 0
        sets_b = 0
        for sc_a, sc_b in zip(score_cells_a, score_cells_b):
            score_a = _read_set_score(sc_a)
            score_b = _read_set_score(sc_b)
            if score_a is None or score_b is None:
                continue
            if score_a >= 6 and score_a > score_b:
                sets_a += 1
            elif score_b >= 6 and score_b > score_a:
                sets_b += 1

        total_sets = sets_a + sets_b
        if total_sets < 2:
            i += 2
            continue

        winner = player_a if sets_a > sets_b else player_b

        results.append({
            "player_a": player_a,
            "player_b": player_b,
            "winner": winner,
            "tournament": current_tournament,
        })
        i += 2

    log.info("Scraped %d completed results from TennisExplorer", len(results))
    return results


# ── Predictions ───────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tennis_model.predictor import TennisModelPredictor  # noqa: E402


def _find_player_elo(name: str, elo_data: dict) -> Optional[dict]:
    """Look up a player's Elo entry from the ratings dict.

    Tries direct key lookup first, then last-name search, then uses the first
    initial to disambiguate when multiple players share a last name.

    Args:
        name:     Player name (TennisExplorer format, e.g. "Salkova D.").
        elo_data: Dict mapping player name strings to Elo rating dicts.

    Returns:
        The matching Elo entry dict, or None if not found.
    """
    norm_target = _normalize(name)

    # Direct lookup
    for key, entry in elo_data.items():
        if _normalize(key) == norm_target:
            return entry

    # Last-name search
    last_target, init_target = _extract_last_and_initial(name)
    if not last_target:
        return None

    candidates = []
    for key, entry in elo_data.items():
        last_key, init_key = _extract_last_and_initial(key)
        if last_key == last_target:
            candidates.append((init_key, entry))

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0][1]

    # Disambiguate by first initial
    if init_target:
        for init_key, entry in candidates:
            if init_key == init_target:
                return entry

    # Fallback: return first candidate
    return candidates[0][1]


def _build_model_input(elo_entry: dict, surface: str) -> dict:
    """Build the pa_* keyword arguments for TennisModelPredictor.predict_match.

    Reads rich player profile from rebuild_player_profiles.py output.
    Falls back to neutral defaults for any missing fields.

    Args:
        elo_entry: Rich player profile dict with Elo, form, serve, return, etc.
        surface:   One of "hard", "clay", "grass".

    Returns:
        Dict with all pa_* model input keys.
    """
    overall = float(elo_entry.get("overall", 1500.0))
    surface_elo = float(elo_entry.get(surface, overall))

    # Surface-specific stats (nested dicts keyed by surface)
    surface_form_dict = elo_entry.get("surface_form", {})
    surface_exp_dict = elo_entry.get("surface_exp", {})
    surface_wr_dict = elo_entry.get("surface_wr", {})

    # Serve/return — use directly if dict, otherwise defaults
    serve = elo_entry.get("serve", {})
    if not isinstance(serve, dict):
        serve = {}
    ret = elo_entry.get("return", {})
    if not isinstance(ret, dict):
        ret = {}

    # Fatigue — use directly if dict, otherwise defaults
    fatigue = elo_entry.get("fatigue", {})
    if not isinstance(fatigue, dict):
        fatigue = {}

    return {
        "pa_elo": overall,
        "pa_surface_elo": surface_elo,
        "pa_rank": int(elo_entry.get("rank", 200)),
        "pa_age": float(elo_entry.get("age", 25.0)),
        "pa_height": float(elo_entry.get("height", 183.0)),
        "pa_hand": elo_entry.get("hand", "R"),
        "pa_matches_played": int(elo_entry.get("matches", 0)),
        "pa_form5": float(elo_entry.get("form5", 0.5)),
        "pa_form10": float(elo_entry.get("form10", 0.5)),
        "pa_form20": float(elo_entry.get("form20", 0.5)),
        "pa_surface_form": float(surface_form_dict.get(surface, 0.5)),
        "pa_qa_form": float(elo_entry.get("qa_form", 0.5)),
        "pa_serve": {
            "ace_rate": float(serve.get("ace_rate", 0.0)),
            "df_rate": float(serve.get("df_rate", 0.0)),
            "first_in_pct": float(serve.get("first_in_pct", 0.0)),
            "first_won_pct": float(serve.get("first_won_pct", 0.0)),
            "second_won_pct": float(serve.get("second_won_pct", 0.0)),
            "bp_saved_pct": float(serve.get("bp_saved_pct", 0.0)),
        },
        "pa_return": {
            "bp_converted_pct": float(ret.get("bp_converted_pct", 0.0)),
            "return_pts_won_pct": float(ret.get("return_pts_won_pct", 0.0)),
        },
        "pa_hold_pct": float(elo_entry.get("hold_pct", 0.8)),
        "pa_surface_exp": int(surface_exp_dict.get(surface, 20)),
        "pa_surface_wr": float(surface_wr_dict.get(surface, 0.5)),
        "pa_fatigue": {
            "matches_7d": int(fatigue.get("matches_7d", 0)),
            "matches_14d": int(fatigue.get("matches_14d", 0)),
            "sets_14d": int(fatigue.get("sets_14d", 0)),
            "days_since_last": int(fatigue.get("days_since_last", 5)),
        },
        "pa_rank_momentum": float(elo_entry.get("rank_momentum", 0.0)),
        "pa_entry": elo_entry.get("entry", ""),
    }


def run_predictions(matches: list[dict]) -> list[dict]:
    """Run model predictions on a list of scheduled matches.

    Loads the model and Elo data, predicts each match, then filters by
    confidence, fair odds range, and round.  Passes through matches with
    round == "unknown".

    Args:
        matches: List of match dicts from scrape_scheduled_matches().

    Returns:
        List of pick dicts sorted by confidence descending.  Each pick has
        keys: player_a, player_b, tournament, surface, round, pick,
        opponent, prob, confidence, fair_odds.
    """
    predictor = TennisModelPredictor()
    predictor.MIN_CONFIDENCE = 0.0
    if not predictor.load():
        log.error("Failed to load tennis model — skipping predictions")
        return []

    if not ELO_FILE.exists():
        log.error("Elo file not found at %s — skipping predictions", ELO_FILE)
        return []

    with ELO_FILE.open("r", encoding="utf-8") as fh:
        elo_data = json.load(fh)

    picks: list[dict] = []
    for match in matches:
        pa_name = match["player_a"]
        pb_name = match["player_b"]
        surface = match.get("surface", "hard")
        match_round = match.get("round", "unknown")

        elo_a = _find_player_elo(pa_name, elo_data)
        elo_b = _find_player_elo(pb_name, elo_data)

        if elo_a is None or elo_b is None:
            log.debug(
                "Elo not found for %s or %s — skipping",
                pa_name, pb_name,
            )
            continue

        pa_kwargs = _build_model_input(elo_a, surface)
        pb_kwargs = {k.replace("pa_", "pb_", 1): v for k, v in _build_model_input(elo_b, surface).items()}

        try:
            result = predictor.predict_match(
                **pa_kwargs,
                **pb_kwargs,
                surface=surface,
            )
        except Exception as exc:
            log.warning("predict_match failed for %s vs %s: %s", pa_name, pb_name, exc)
            continue

        if result is None:
            continue

        confidence = result.get("confidence", 0.0)
        pick_player = result.get("pick")  # "a" or "b"
        prob_a = result.get("prob_a", 0.5)
        prob_b = result.get("prob_b", 0.5)

        if pick_player == "a":
            pick_name = pa_name
            opponent_name = pb_name
            prob = prob_a
        else:
            pick_name = pb_name
            opponent_name = pa_name
            prob = prob_b

        fair_odds = 1.0 / prob if prob > 0 else 99.0

        # Filter by confidence
        if confidence < MIN_CONFIDENCE:
            continue

        # Filter by fair odds range
        if not (MIN_ODDS <= fair_odds <= MAX_ODDS):
            continue

        # Filter by round (pass through "unknown")
        if match_round != "unknown" and match_round not in ROUNDS_FILTER:
            continue

        picks.append({
            "player_a": pa_name,
            "player_b": pb_name,
            "tournament": match.get("tournament", ""),
            "surface": surface,
            "round": match_round,
            "pick": pick_name,
            "opponent": opponent_name,
            "prob": prob,
            "confidence": confidence,
            "fair_odds": fair_odds,
        })

    picks.sort(key=lambda x: x["confidence"], reverse=True)
    log.info("run_predictions: %d qualified picks from %d matches", len(picks), len(matches))
    return picks


# ── Scan Loop ─────────────────────────────────────────────────────────────────

def run_scan(state: dict, executor: TennisExecutor) -> dict:
    """Scan SX Bet tennis markets, run model predictions, place orders.

    SX Bet is the source of truth for available markets. For each market:
    1. Look up both players in Elo DB
    2. Run LightGBM prediction
    3. Filter by confidence + odds range
    4. Fetch orderbook odds from SX Bet
    5. Hand off to the executor (paper recording or live order)

    Args:
        state: Current bot state dict (mutated in place).

    Returns:
        Updated state dict.
    """
    from tennis_sxbet import TennisSXBet

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    if state.get("today_date") != today_str:
        state["today_date"] = today_str
        state["today_bets"] = 0
        log.info("New trading day %s — daily bet counter reset", today_str)

    log.info("run_scan: starting scan at %s UTC", now_utc.isoformat())

    # 1. Fetch all SX Bet tennis match winner markets
    try:
        sxbet = TennisSXBet()
        sxbet_markets = sxbet.get_all_tennis_markets()
    except Exception as exc:
        log.error("run_scan: failed to fetch SX Bet markets: %s", exc)
        state["last_scan"] = now_utc.isoformat()
        return state

    log.info("run_scan: %d SX Bet match winner markets found", len(sxbet_markets))

    if not sxbet_markets:
        state["last_scan"] = now_utc.isoformat()
        return state

    # 1b. Scrape TennisExplorer to get round info per match (SX Bet doesn't expose round).
    # Build a map keyed by sorted (last_name_a, last_name_b) → round.
    te_round_map: dict[tuple[str, str], str] = {}
    try:
        te_matches = scrape_scheduled_matches()
        for m in te_matches:
            la, _ = _extract_last_and_initial(m["player_a"])
            lb, _ = _extract_last_and_initial(m["player_b"])
            if not la or not lb:
                continue
            key = tuple(sorted([la, lb]))
            te_round_map[key] = m.get("round", "unknown")
        log.info("run_scan: built round map from %d TennisExplorer matches", len(te_round_map))
    except Exception as exc:
        log.warning("run_scan: TennisExplorer round scrape failed: %s", exc)

    # 2. Load Elo data + model
    elo_data = {}
    if ELO_FILE.exists():
        try:
            elo_data = json.loads(ELO_FILE.read_text())
        except Exception:
            log.warning("Failed to load Elo data")

    predictor = TennisModelPredictor()
    if not predictor.load():
        log.error("Failed to load tennis model — skipping predictions")
        state["last_scan"] = now_utc.isoformat()
        return state
    predictor.MIN_CONFIDENCE = 0.0  # We filter ourselves

    # 3. For each SX Bet market: look up Elo, predict, filter, trade
    picks_found = 0
    skipped_no_elo = 0
    skipped_low_conf = 0
    skipped_round = 0

    for market in sxbet_markets:
        if state["today_bets"] >= MAX_DAILY_BETS:
            log.info("run_scan: MAX_DAILY_BETS (%d) reached", MAX_DAILY_BETS)
            break

        player_a = market["player_a"]  # SX Bet full names
        player_b = market["player_b"]
        league = market["league"]

        # Detect surface from league name
        surface = _detect_surface(league)

        # Round filter — look up via TennisExplorer. Pass through "unknown" because
        # _detect_round requires a tournament-name suffix TE doesn't emit; soft filter
        # only blocks when we have a positive identification of a non-target round.
        la, _ = _extract_last_and_initial(player_a)
        lb, _ = _extract_last_and_initial(player_b)
        round_key = tuple(sorted([la, lb]))
        match_round = te_round_map.get(round_key, "unknown")
        if match_round != "unknown" and match_round not in ROUNDS_FILTER:
            skipped_round += 1
            continue

        # Look up both players in Elo DB
        pa_elo = _find_player_elo(player_a, elo_data)
        pb_elo = _find_player_elo(player_b, elo_data)
        if not pa_elo or not pb_elo:
            skipped_no_elo += 1
            continue

        # Run prediction
        try:
            pa_input = _build_model_input(pa_elo, surface)
            pb_input = _build_model_input(pb_elo, surface)
            call_args = dict(pa_input)
            for k, v in pb_input.items():
                call_args["pb_" + k[3:]] = v
            call_args["surface"] = surface
            pred = predictor.predict_match(**call_args)
        except Exception as exc:
            log.debug("Prediction failed for %s vs %s: %s", player_a, player_b, exc)
            continue

        if not pred:
            continue

        prob_a, prob_b = pred["prob_a"], pred["prob_b"]
        best_prob = max(prob_a, prob_b)

        if best_prob < MIN_CONFIDENCE:
            skipped_low_conf += 1
            continue

        # Determine pick
        if prob_a >= prob_b:
            pick_name, opponent_name, pick_prob = player_a, player_b, prob_a
        else:
            pick_name, opponent_name, pick_prob = player_b, player_a, prob_b

        fair_odds = round(1.0 / pick_prob, 3)

        # Fair odds range filter
        if not (MIN_ODDS <= fair_odds <= MAX_ODDS):
            continue

        # Generate pick ID from market_hash (unique + stable across scans)
        pick_id = market["market_hash"]
        if pick_id in state.get("open_picks", {}):
            continue

        # Fetch live odds from SX Bet orderbook
        try:
            odds_info = sxbet.get_best_back_odds(
                market["market_hash"], pick_name, market["player_a"]
            )
        except Exception as exc:
            log.warning("get_best_back_odds failed for %s: %s", pick_id, exc)
            continue

        if odds_info is None:
            append_journal({
                "type": "skipped", "reason": "no_liquidity",
                "pick_id": pick_id, "pick": pick_name, "opponent": opponent_name,
                "league": league, "ts": now_utc.isoformat(),
            }, SKIPPED_FILE)
            continue

        sxbet_odds = float(odds_info["decimal_odds"])

        if not (MIN_ODDS <= sxbet_odds <= MAX_ODDS):
            append_journal({
                "type": "skipped", "reason": "odds_out_of_range",
                "pick_id": pick_id, "pick": pick_name,
                "sxbet_odds": sxbet_odds, "ts": now_utc.isoformat(),
            }, SKIPPED_FILE)
            continue

        implied_prob = 1.0 / sxbet_odds
        edge = round(pick_prob - implied_prob, 4)

        # Negative edge filter — don't paper-trade picks where market price is worse than fair
        if edge < 0:
            append_journal({
                "type": "skipped", "reason": "negative_edge",
                "pick_id": pick_id, "pick": pick_name, "opponent": opponent_name,
                "model_prob": round(pick_prob, 4), "sxbet_odds": sxbet_odds,
                "edge": edge, "ts": now_utc.isoformat(),
            }, SKIPPED_FILE)
            continue

        pick_context = {
            "pick_id": pick_id,
            "pick": pick_name,
            "opponent": opponent_name,
            "league": league,
            "surface": surface,
            "round": match_round,
            "model_prob": round(pick_prob, 4),
            "fair_odds": fair_odds,
            "sxbet_odds": sxbet_odds,
            "sxbet_available_usd": odds_info["available_usd"],
            "implied_prob": round(implied_prob, 4),
            "edge": edge,
            "market_hash": market["market_hash"],
            "is_pick_outcome_one": pick_name.strip() == market["player_a"].strip(),
            "ts": now_utc.isoformat(),
        }

        result = executor.place_order(pick_context)

        if result.status == "blocked":
            append_journal({
                "type": "skipped", "reason": f"executor_block:{result.block_reason}",
                "pick_id": pick_id, "pick": pick_name, "opponent": opponent_name,
                "league": league, "ts": now_utc.isoformat(),
            }, SKIPPED_FILE)
            log.warning("Executor blocked %s: %s", pick_id, result.block_reason)
            continue

        if result.status == "live_failed":
            append_journal({
                "type": "skipped", "reason": "live_order_failed",
                "pick_id": pick_id, "pick": pick_name, "error": result.block_reason,
                "ts": now_utc.isoformat(),
            }, SKIPPED_FILE)
            log.error("Live order failed for %s: %s", pick_id, result.block_reason)
            continue

        trade_entry = result.trade_entry
        state.setdefault("open_picks", {})[pick_id] = trade_entry
        append_journal(trade_entry, JOURNAL_FILE)

        state["today_bets"] = state.get("today_bets", 0) + 1
        state["total_bets"] = state.get("total_bets", 0) + 1
        picks_found += 1

        if result.mode == "live":
            log.info(
                "LIVE FILL: %s @ %.3f (filled $%.2f @ %.3f) | order=%s | %s",
                pick_name, sxbet_odds,
                result.filled_stake_usd or 0.0,
                result.filled_decimal_odds or 0.0,
                (result.order_id or "")[:16], league,
            )
        else:
            log.info(
                "PAPER BET: %s @ %.3f (model: %.0f%%, edge: %.1f%%) $%.0f | %s",
                pick_name, sxbet_odds, pick_prob * 100, edge * 100,
                PAPER_STAKE, league,
            )

    state["last_scan"] = now_utc.isoformat()
    log.info(
        "run_scan: done — %d picks, %d skipped (no Elo: %d, low conf: %d, round: %d), %d open total",
        picks_found, skipped_no_elo + skipped_low_conf + skipped_round,
        skipped_no_elo, skipped_low_conf, skipped_round,
        len(state.get("open_picks", {})),
    )
    return state


# ── Settle Loop ───────────────────────────────────────────────────────────────

def run_settle(state: dict, executor: TennisExecutor) -> dict:
    """Settle open picks against completed match results.

    For each open pick, checks if a completed result is available.  Both the
    pick and opponent must match the result's players (in either order).
    Win/loss P&L is calculated, the balance updated, and the trade journalled.

    Args:
        state: Current bot state dict (mutated in place).

    Returns:
        Updated state dict.
    """
    open_picks: dict = state.get("open_picks", {})
    if not open_picks:
        log.info("run_settle: no open picks — nothing to settle")
        return state

    now_utc = datetime.now(timezone.utc)
    log.info("run_settle: checking %d open picks for results", len(open_picks))

    results = scrape_completed_results()
    if not results:
        log.info("run_settle: no completed results found")
        state["last_settle"] = now_utc.isoformat()
        return state

    settled_ids: list[str] = []
    settlements: list[dict] = []

    for pick_id, pick in list(open_picks.items()):
        pick_player = pick["pick"]
        opponent = pick["opponent"]
        stake = pick.get("stake", PAPER_STAKE)
        sxbet_odds = pick.get("sxbet_odds", 2.0)

        for result in results:
            ra = result["player_a"]
            rb = result["player_b"]

            # Both players must match (either order)
            match_order_1 = (
                match_player_name(pick_player, ra)
                and match_player_name(opponent, rb)
            )
            match_order_2 = (
                match_player_name(pick_player, rb)
                and match_player_name(opponent, ra)
            )
            if not (match_order_1 or match_order_2):
                continue

            won = match_player_name(pick_player, result["winner"])
            reconciled = executor.reconcile_pick(pick, won=won)
            pnl = float(reconciled["pnl"])
            outcome = reconciled["outcome"]

            state["balance"] = round(state.get("balance", STARTING_BALANCE) + pnl, 2)
            state["total_pnl"] = round(state.get("total_pnl", 0.0) + pnl, 2)
            if won:
                state["wins"] = state.get("wins", 0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1

            settlement = {
                "type": "settled",
                "pick_id": pick_id,
                "pick": pick_player,
                "opponent": opponent,
                "outcome": outcome,
                "pnl": round(pnl, 2),
                "sxbet_odds": sxbet_odds,
                "stake": stake,
                "balance": round(state["balance"], 2),
                "total_pnl": round(state["total_pnl"], 2),
                "result_winner": result["winner"],
                "tournament": result.get("tournament"),
                "mode": reconciled.get("mode", "dry_run"),
                "ts": now_utc.isoformat(),
            }
            if "divergence_usd" in reconciled:
                settlement["divergence_usd"] = reconciled["divergence_usd"]
                settlement["filled_decimal_odds"] = reconciled.get("filled_decimal_odds")
                settlement["filled_stake_usd"] = reconciled.get("filled_stake_usd")

            append_journal(settlement, JOURNAL_FILE)
            settled_ids.append(pick_id)
            settlements.append(settlement)

            log.info(
                "run_settle: %s %s vs %s | %s | pnl=%.2f balance=%.2f",
                outcome.upper(), pick_player, opponent,
                pick_id, pnl, state["balance"],
            )
            break  # matched — move to next open pick

    # Remove settled picks
    for pick_id in settled_ids:
        open_picks.pop(pick_id, None)
    state["open_picks"] = open_picks

    # Append daily summary if any settlements occurred
    if settlements:
        today_str = now_utc.strftime("%Y-%m-%d")
        daily_entry = {
            "date": today_str,
            "settled_count": len(settlements),
            "wins": sum(1 for s in settlements if s["outcome"] == "win"),
            "losses": sum(1 for s in settlements if s["outcome"] == "loss"),
            "daily_pnl": round(sum(s["pnl"] for s in settlements), 2),
            "balance": round(state["balance"], 2),
            "total_pnl": round(state["total_pnl"], 2),
            "ts": now_utc.isoformat(),
        }
        append_journal(daily_entry, DAILY_FILE)
        log.info(
            "run_settle: daily summary — %d settled, pnl=%.2f",
            len(settlements), daily_entry["daily_pnl"],
        )

    state["last_settle"] = now_utc.isoformat()
    return state


# ── Main Loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: run the scan/settle loop indefinitely.

    Scans at each hour listed in SCAN_TIMES_UTC and settles at SETTLE_INTERVAL
    second intervals whenever there are open picks.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    config = ExecutorConfig.from_env(STATE_DIR)
    executor = TennisExecutor(config)

    banner_mode = config.mode.upper()
    log.info("=" * 72)
    log.info("Tennis bot starting in %s mode", banner_mode)
    if config.mode == "live":
        log.warning(
            "LIVE MODE ACTIVE — orders will be signed and submitted. "
            "Wallet=%s caps: daily=$%.2f, match_liability=$%.2f, kill_switch=%s",
            config.wallet_address[:10] + "..." if config.wallet_address else "(unset)",
            config.max_daily_stake_usd,
            config.max_match_liability_usd,
            config.kill_switch_path,
        )
    log.info(
        "scan_times_utc=%s settle_interval=%ds min_confidence=%.2f "
        "stake=$%.2f max_daily_bets=%d",
        SCAN_TIMES_UTC, SETTLE_INTERVAL, MIN_CONFIDENCE,
        config.base_stake_usd, MAX_DAILY_BETS,
    )
    log.info("=" * 72)

    state = load_state()
    executor.set_today_live_stake(state.get("today_live_stake", 0.0))

    scanned_hours: set[int] = set()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            current_hour = now_utc.hour

            # Reset scanned_hours at midnight
            today_str = now_utc.strftime("%Y-%m-%d")
            if state.get("today_date") != today_str:
                scanned_hours.clear()

            # Scan check
            if current_hour in SCAN_TIMES_UTC and current_hour not in scanned_hours:
                log.info("main: scan window — hour %d UTC", current_hour)
                state = run_scan(state, executor)
                save_state(state)
                scanned_hours.add(current_hour)

            # Settle check
            last_settle_str = state.get("last_settle")
            open_count = len(state.get("open_picks", {}))
            if open_count > 0:
                if last_settle_str is None:
                    should_settle = True
                else:
                    last_settle_dt = datetime.fromisoformat(last_settle_str)
                    seconds_since = (now_utc - last_settle_dt).total_seconds()
                    should_settle = seconds_since >= SETTLE_INTERVAL

                if should_settle:
                    log.info("main: settle check — %d open picks", open_count)
                    state = run_settle(state, executor)
                    save_state(state)

            time.sleep(60)

        except KeyboardInterrupt:
            log.info("main: KeyboardInterrupt received — saving state and exiting")
            save_state(state)
            break
        except Exception as exc:
            log.error("main: unhandled exception: %s", exc, exc_info=True)
            time.sleep(300)


if __name__ == "__main__":
    main()
