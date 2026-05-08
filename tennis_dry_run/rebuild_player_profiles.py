"""
rebuild_player_profiles.py — Build a rich elo_ratings.json from Sackmann match CSVs.

Processes ATP + WTA match data from 2010–2026, tracking per-player rolling stats
(Elo, form, serve/return, fatigue, surface affinity) and outputs a JSON file the
LightGBM predictor's _build_model_input() can consume directly.

The PlayerTracker class is adapted from LXII Vegas tools/build_tennis_model.py.

Usage:
    python tennis_dry_run/rebuild_player_profiles.py
    python tennis_dry_run/rebuild_player_profiles.py --data-dir /path/to/csvs
    python tennis_dry_run/rebuild_player_profiles.py --data-dir /opt/tennis-dry-run/data/tennis --output /opt/tennis-dry-run/elo_ratings.json
"""

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Elo constants (must match build_tennis_model.py)
# ---------------------------------------------------------------------------
ELO_INIT = 1500
ELO_K = 32
ELO_K_SLAM = 48
SURFACE_ELO_K = 40

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_score(score: str) -> dict:
    """Parse a tennis score string into structured data.

    Returns a dict with:
        sets_w, sets_l, games_w, games_l, tiebreaks,
        won_first_set, lost_first_set, came_from_behind,
        walkover, retired, is_straight_sets, bagels
    """
    empty = {
        "sets_w": 0, "sets_l": 0, "games_w": 0, "games_l": 0,
        "tiebreaks": 0, "won_first_set": False, "lost_first_set": False,
        "came_from_behind": False, "walkover": False,
        "retired": False, "is_straight_sets": False, "bagels": 0,
    }
    if not score:
        return empty

    score_lower = score.lower()
    walkover = "w/o" in score_lower or "walkover" in score_lower
    retired = any(x in score_lower for x in ["ret", "def", "abd"])

    if walkover:
        return {**empty, "walkover": True}

    sets_w = sets_l = tiebreaks = games_w = games_l = bagels = 0
    set_scores: list[tuple[int, int]] = []

    for part in score.split():
        part = part.strip()
        if not part or part.lower() in ("ret", "ret.", "def.", "abd.", "w/o", "walkover"):
            continue
        if "(" in part:
            tiebreaks += 1
        # Strip tiebreak score
        digits = part.replace("(", "-").replace(")", "").split("-")
        if len(digits) >= 2:
            try:
                g1, g2 = int(digits[0]), int(digits[1])
                games_w += g1
                games_l += g2
                set_scores.append((g1, g2))
                if g1 > g2:
                    sets_w += 1
                    if g2 <= 1:
                        bagels += 1
                else:
                    sets_l += 1
            except ValueError:
                pass

    won_first_set = bool(set_scores and set_scores[0][0] > set_scores[0][1])
    is_straight_sets = sets_w > 0 and sets_l == 0

    return {
        "sets_w": sets_w, "sets_l": sets_l,
        "games_w": games_w, "games_l": games_l,
        "tiebreaks": tiebreaks,
        "won_first_set": won_first_set,
        "lost_first_set": not won_first_set and bool(set_scores),
        "came_from_behind": sets_l > 0 and sets_w > sets_l,
        "walkover": False,
        "retired": retired,
        "is_straight_sets": is_straight_sets,
        "bagels": bagels,
    }


# ---------------------------------------------------------------------------
# PlayerTracker — adapted from LXII Vegas/tools/build_tennis_model.py
# ---------------------------------------------------------------------------

class PlayerTracker:
    """Tracks rolling statistics for a single player across all processed matches."""

    def __init__(self):
        # Elo
        self.elo_overall: float = ELO_INIT
        self.elo_surface: defaultdict = defaultdict(lambda: ELO_INIT)
        self.elo_history: list[float] = []

        # Form
        # Each element: (date_str, won: bool, surface: str, tourney_level: str, opp_elo: float)
        self.results: list[tuple] = []
        self.matches_played: int = 0

        # Serve / return stats (rolling window of dicts)
        self.serve_stats: list[dict] = []
        self.return_stats: list[dict] = []
        self.surface_serve_stats: defaultdict = defaultdict(list)

        # Surface results: surface -> [(date_str, won)]
        self.surface_results: defaultdict = defaultdict(list)

        # Fatigue
        self.recent_match_dates: list[datetime] = []
        self.recent_sets_played: list[tuple] = []  # (datetime, num_sets)

        # Ranking
        self.ranking_history: list[tuple] = []  # (date_str, rank)

        # Hold stats: (sv_games, sv_games_held)
        self.hold_stats: list[tuple] = []

        # Profile
        self.hand: str = ""
        self.height: float = 0.0
        self.country: str = ""
        self.last_age: float = 25.0
        self.last_rank: int = 0
        self.last_name: str = ""
        self.entry_type: str = ""

        # Score detail stats
        self.match_scores: list[tuple] = []  # (date, gw, gl, sw, sl, mins, surface, opp_elo)
        self.first_set_results: list[bool] = []
        self.comebacks_from_set_down: int = 0
        self.sets_down_count: int = 0
        self.tiebreak_wins: int = 0
        self.tiebreak_total: int = 0
        self.straight_sets_wins: int = 0
        self.straight_sets_total: int = 0
        self.bagel_breadstick_count: int = 0
        self.total_sets_won: int = 0
        self.last_match_minutes: float = 0.0
        self.last_match_sets: int = 0

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    def get_form(self, surface: str | None = None, n: int = 10) -> float:
        relevant = self.results if not surface else [r for r in self.results if r[2] == surface]
        recent = relevant[-n:]
        if not recent:
            return 0.5
        return sum(1 for r in recent if r[1]) / len(recent)

    def get_avg_serve_stats(self, n: int = 10) -> dict:
        recent = self.serve_stats[-n:]
        if not recent:
            return {
                "ace_rate": 0.0, "df_rate": 0.0, "first_in_pct": 0.0,
                "first_won_pct": 0.0, "second_won_pct": 0.0, "bp_saved_pct": 0.0,
            }
        return {k: float(np.mean([s[k] for s in recent])) for k in recent[0]}

    def get_avg_return_stats(self, n: int = 10) -> dict:
        recent = self.return_stats[-n:]
        if not recent:
            return {"bp_converted_pct": 0.0, "return_pts_won_pct": 0.0}
        return {k: float(np.mean([s[k] for s in recent])) for k in recent[0]}

    def get_quality_adjusted_form(self, n: int = 10) -> float:
        recent = self.results[-n:]
        if not recent:
            return 0.5
        weighted_sum = 0.0
        weight_total = 0.0
        for _, won, _, _, opp_elo in recent:
            w = max(opp_elo - 1300, 100) / 200.0
            weighted_sum += w * (1.0 if won else 0.0)
            weight_total += w
        return weighted_sum / weight_total if weight_total > 0 else 0.5

    def get_hold_pct(self, n: int = 10) -> float:
        recent = self.hold_stats[-n:]
        if not recent:
            return 0.8
        total_games = sum(g for g, _ in recent)
        total_held = sum(h for _, h in recent)
        return total_held / max(total_games, 1)

    def get_rank_momentum(self) -> float:
        if len(self.ranking_history) < 2:
            return 0.0
        old_rank = self.ranking_history[0][1]
        new_rank = self.ranking_history[-1][1]
        if old_rank <= 0 or new_rank <= 0:
            return 0.0
        return (old_rank - new_rank) / max(old_rank, 1)

    def get_surface_form(self) -> dict:
        """Win rate per surface (all-time)."""
        out = {}
        for surf, res in self.surface_results.items():
            if res:
                out[surf] = round(sum(1 for _, w in res if w) / len(res), 4)
        return out

    def get_surface_exp(self) -> dict:
        """Match count per surface."""
        return {surf: len(res) for surf, res in self.surface_results.items()}

    def get_fatigue(self, reference_date: datetime) -> dict:
        matches_7d = sum(1 for d in self.recent_match_dates if 0 < (reference_date - d).days <= 7)
        matches_14d = sum(1 for d in self.recent_match_dates if 0 < (reference_date - d).days <= 14)
        sets_14d = sum(s for d, s in self.recent_sets_played if 0 < (reference_date - d).days <= 14)
        days_since_last = 90
        if self.recent_match_dates:
            last = max(self.recent_match_dates)
            days_since_last = max(0, (reference_date - last).days)
        return {
            "matches_7d": matches_7d,
            "matches_14d": matches_14d,
            "sets_14d": sets_14d,
            "days_since_last": min(days_since_last, 90),
        }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_match_csvs(data_dir: Path, start_year: int = 2010, end_year: int = 2026) -> list[dict]:
    """Load all ATP + WTA tour-level match CSVs from data_dir.

    Skips qualifiers/challengers (atp_matches_qual_chall_* files).
    """
    all_matches: list[dict] = []
    prefixes = ["atp_matches", "wta_matches"]

    for prefix in prefixes:
        for year in range(start_year, end_year + 1):
            csv_path = data_dir / f"{prefix}_{year}.csv"
            if not csv_path.exists():
                logger.debug("Not found, skipping: %s", csv_path)
                continue
            try:
                with open(csv_path, encoding="utf-8", errors="replace") as fh:
                    reader = csv.DictReader(fh)
                    rows = list(reader)
                all_matches.extend(rows)
                logger.info("Loaded %s %d: %d matches", prefix, year, len(rows))
            except Exception as exc:
                logger.warning("Error loading %s: %s", csv_path, exc)

    logger.info("Total matches loaded: %d", len(all_matches))
    return all_matches


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_matches(matches: list[dict]) -> dict[str, PlayerTracker]:
    """Process all matches chronologically, updating per-player state.

    Returns: dict mapping player_id -> PlayerTracker
    Also maintains player_id -> latest name mapping for output.
    """
    # Sort chronologically (tourney_date is YYYYMMDD, match_num breaks ties)
    matches.sort(key=lambda m: m.get("tourney_date", "0") + m.get("match_num", "0").zfill(5))

    players: dict[str, PlayerTracker] = defaultdict(PlayerTracker)
    # Maps player_id -> most recently seen full name
    id_to_name: dict[str, str] = {}

    total = len(matches)
    for idx, m in enumerate(matches):
        winner_id = m.get("winner_id", "").strip()
        loser_id = m.get("loser_id", "").strip()
        winner_name = m.get("winner_name", "").strip()
        loser_name = m.get("loser_name", "").strip()

        if not winner_id or not loser_id or winner_id == loser_id:
            continue

        # Track latest names
        if winner_name:
            id_to_name[winner_id] = winner_name
        if loser_name:
            id_to_name[loser_id] = loser_name

        surface = m.get("surface", "Hard").strip().lower()
        tourney_date = m.get("tourney_date", "").strip()
        tourney_level = m.get("tourney_level", "A").strip()
        score = m.get("score", "").strip()
        minutes = _safe_float(m.get("minutes"))
        best_of = _safe_int(m.get("best_of"), 3)

        score_info = _parse_score(score)
        if score_info["walkover"]:
            continue  # Skip walkovers

        w = players[winner_id]
        l = players[loser_id]

        # Profile update — always refresh from latest match data
        if m.get("winner_hand"):
            w.hand = m["winner_hand"]
        w.height = _safe_float(m.get("winner_ht"), w.height)
        w.country = m.get("winner_ioc", w.country) or w.country
        w.last_age = _safe_float(m.get("winner_age"), w.last_age)
        w_rank = _safe_int(m.get("winner_rank"))
        if w_rank > 0:
            w.last_rank = w_rank

        if m.get("loser_hand"):
            l.hand = m["loser_hand"]
        l.height = _safe_float(m.get("loser_ht"), l.height)
        l.country = m.get("loser_ioc", l.country) or l.country
        l.last_age = _safe_float(m.get("loser_age"), l.last_age)
        l_rank = _safe_int(m.get("loser_rank"))
        if l_rank > 0:
            l.last_rank = l_rank

        # Entry types
        w.entry_type = m.get("winner_entry", w.entry_type) or w.entry_type
        l.entry_type = m.get("loser_entry", l.entry_type) or l.entry_type

        # Most recent names
        w.last_name = winner_name or w.last_name
        l.last_name = loser_name or l.last_name

        # === UPDATE ELO ===
        k = ELO_K_SLAM if tourney_level == "G" else ELO_K
        exp_w = 1.0 / (1.0 + 10 ** ((l.elo_overall - w.elo_overall) / 400))
        w.elo_overall += k * (1 - exp_w)
        l.elo_overall += k * (0 - (1 - exp_w))
        w.elo_history.append(w.elo_overall)
        l.elo_history.append(l.elo_overall)
        if len(w.elo_history) > 20:
            w.elo_history = w.elo_history[-20:]
        if len(l.elo_history) > 20:
            l.elo_history = l.elo_history[-20:]

        # Surface Elo
        exp_s = 1.0 / (1.0 + 10 ** ((l.elo_surface[surface] - w.elo_surface[surface]) / 400))
        w.elo_surface[surface] += SURFACE_ELO_K * (1 - exp_s)
        l.elo_surface[surface] += SURFACE_ELO_K * (0 - (1 - exp_s))

        # === FORM ===
        w.results.append((tourney_date, True, surface, tourney_level, l.elo_overall))
        l.results.append((tourney_date, False, surface, tourney_level, w.elo_overall))
        w.matches_played += 1
        l.matches_played += 1

        # Surface results
        w.surface_results[surface].append((tourney_date, True))
        l.surface_results[surface].append((tourney_date, False))

        # === SERVE STATS ===
        w_svpt = _safe_int(m.get("w_svpt"))
        if w_svpt > 0:
            w_1stIn = _safe_int(m.get("w_1stIn"))
            w_1stWon = _safe_int(m.get("w_1stWon"))
            w_2ndWon = _safe_int(m.get("w_2ndWon"))
            w_bpSaved = _safe_int(m.get("w_bpSaved"))
            w_bpFaced = _safe_int(m.get("w_bpFaced"))
            w_serve = {
                "ace_rate": _safe_int(m.get("w_ace")) / w_svpt,
                "df_rate": _safe_int(m.get("w_df")) / w_svpt,
                "first_in_pct": w_1stIn / w_svpt,
                "first_won_pct": w_1stWon / max(w_1stIn, 1),
                "second_won_pct": w_2ndWon / max(w_svpt - w_1stIn, 1),
                "bp_saved_pct": w_bpSaved / max(w_bpFaced, 1),
            }
            w.serve_stats.append(w_serve)
            if len(w.serve_stats) > 30:
                w.serve_stats = w.serve_stats[-30:]
            w.surface_serve_stats[surface].append(w_serve)
            if len(w.surface_serve_stats[surface]) > 20:
                w.surface_serve_stats[surface] = w.surface_serve_stats[surface][-20:]

        l_svpt = _safe_int(m.get("l_svpt"))
        if l_svpt > 0:
            l_1stIn = _safe_int(m.get("l_1stIn"))
            l_1stWon = _safe_int(m.get("l_1stWon"))
            l_2ndWon = _safe_int(m.get("l_2ndWon"))
            l_bpSaved = _safe_int(m.get("l_bpSaved"))
            l_bpFaced = _safe_int(m.get("l_bpFaced"))
            l_serve = {
                "ace_rate": _safe_int(m.get("l_ace")) / l_svpt,
                "df_rate": _safe_int(m.get("l_df")) / l_svpt,
                "first_in_pct": l_1stIn / l_svpt,
                "first_won_pct": l_1stWon / max(l_1stIn, 1),
                "second_won_pct": l_2ndWon / max(l_svpt - l_1stIn, 1),
                "bp_saved_pct": l_bpSaved / max(l_bpFaced, 1),
            }
            l.serve_stats.append(l_serve)
            if len(l.serve_stats) > 30:
                l.serve_stats = l.serve_stats[-30:]
            l.surface_serve_stats[surface].append(l_serve)
            if len(l.surface_serve_stats[surface]) > 20:
                l.surface_serve_stats[surface] = l.surface_serve_stats[surface][-20:]

        # === RETURN STATS (derived from opponent's serve) ===
        if l_svpt > 0:
            w.return_stats.append({
                "bp_converted_pct": (l_bpFaced - l_bpSaved) / max(l_bpFaced, 1),
                "return_pts_won_pct": (l_svpt - l_1stWon - l_2ndWon) / l_svpt,
            })
            if len(w.return_stats) > 30:
                w.return_stats = w.return_stats[-30:]

        if w_svpt > 0:
            l.return_stats.append({
                "bp_converted_pct": (w_bpFaced - w_bpSaved) / max(w_bpFaced, 1),
                "return_pts_won_pct": (w_svpt - w_1stWon - w_2ndWon) / w_svpt,
            })
            if len(l.return_stats) > 30:
                l.return_stats = l.return_stats[-30:]

        # === HOLD STATS ===
        w_sv_games = _safe_int(m.get("w_SvGms"))
        l_sv_games = _safe_int(m.get("l_SvGms"))
        if w_svpt > 0 and w_sv_games > 0:
            w_breaks_conceded = w_bpFaced - w_bpSaved
            w.hold_stats.append((w_sv_games, max(0, w_sv_games - w_breaks_conceded)))
            if len(w.hold_stats) > 20:
                w.hold_stats = w.hold_stats[-20:]
        if l_svpt > 0 and l_sv_games > 0:
            l_breaks_conceded = l_bpFaced - l_bpSaved
            l.hold_stats.append((l_sv_games, max(0, l_sv_games - l_breaks_conceded)))
            if len(l.hold_stats) > 20:
                l.hold_stats = l.hold_stats[-20:]

        # === FATIGUE TRACKING ===
        total_sets = score_info["sets_w"] + score_info["sets_l"]
        try:
            md = datetime.strptime(tourney_date, "%Y%m%d")
            w.recent_match_dates.append(md)
            l.recent_match_dates.append(md)
            w.recent_sets_played.append((md, total_sets))
            l.recent_sets_played.append((md, total_sets))
            # Keep last 60 days
            cutoff = md - timedelta(days=60)
            w.recent_match_dates = [d for d in w.recent_match_dates if d > cutoff]
            l.recent_match_dates = [d for d in l.recent_match_dates if d > cutoff]
            w.recent_sets_played = [(d, s) for d, s in w.recent_sets_played if d > cutoff]
            l.recent_sets_played = [(d, s) for d, s in l.recent_sets_played if d > cutoff]
        except (ValueError, TypeError):
            pass

        # === RANKING HISTORY ===
        if w_rank > 0:
            w.ranking_history.append((tourney_date, w_rank))
            if len(w.ranking_history) > 10:
                w.ranking_history = w.ranking_history[-10:]
        if l_rank > 0:
            l.ranking_history.append((tourney_date, l_rank))
            if len(l.ranking_history) > 10:
                l.ranking_history = l.ranking_history[-10:]

        # === SCORE DETAIL ===
        w.match_scores.append((tourney_date, score_info["games_w"], score_info["games_l"],
                               score_info["sets_w"], score_info["sets_l"], minutes, surface, l.elo_overall))
        l.match_scores.append((tourney_date, score_info["games_l"], score_info["games_w"],
                               score_info["sets_l"], score_info["sets_w"], minutes, surface, w.elo_overall))
        if len(w.match_scores) > 30:
            w.match_scores = w.match_scores[-30:]
        if len(l.match_scores) > 30:
            l.match_scores = l.match_scores[-30:]

        # First set results
        if score_info["won_first_set"] or score_info["lost_first_set"]:
            w.first_set_results.append(score_info["won_first_set"])
            l.first_set_results.append(not score_info["won_first_set"])
            if len(w.first_set_results) > 30:
                w.first_set_results = w.first_set_results[-30:]
            if len(l.first_set_results) > 30:
                l.first_set_results = l.first_set_results[-30:]

        # Comebacks
        if score_info.get("lost_first_set"):
            w.sets_down_count += 1
            if score_info["came_from_behind"]:
                w.comebacks_from_set_down += 1
        if score_info["sets_l"] > 0:
            l.sets_down_count += 1

        # Tiebreaks
        if score_info["tiebreaks"] > 0:
            tbs = score_info["tiebreaks"]
            w.tiebreak_total += tbs
            l.tiebreak_total += tbs
            w.tiebreak_wins += max(1, tbs // 2 + 1)
            l.tiebreak_wins += max(0, tbs // 2)

        # Straight sets + bagels
        if score_info["sets_w"] > 0:
            w.straight_sets_total += 1
            if score_info["is_straight_sets"]:
                w.straight_sets_wins += 1
            w.total_sets_won += score_info["sets_w"]
            w.bagel_breadstick_count += score_info["bagels"]
            l.total_sets_won += score_info["sets_l"]

        # Last match difficulty
        w.last_match_minutes = minutes
        w.last_match_sets = total_sets
        l.last_match_minutes = minutes
        l.last_match_sets = total_sets

        if (idx + 1) % 10_000 == 0:
            logger.info("  Processed %d/%d matches (%d players tracked)", idx + 1, total, len(players))

    logger.info("Processing complete. Tracked %d unique players.", len(players))
    return players, id_to_name


# ---------------------------------------------------------------------------
# Output serialisation
# ---------------------------------------------------------------------------

def build_output(
    players: dict[str, "PlayerTracker"],
    id_to_name: dict[str, str],
    reference_date: datetime,
    min_matches: int = 5,
) -> dict:
    """Snapshot each player's current state into the output dict.

    Keys are player FULL NAMES (what SX Bet / the dry-run bot uses for lookup).
    """
    output: dict[str, dict] = {}
    skipped_no_name = 0
    skipped_few_matches = 0

    for player_id, tracker in players.items():
        if tracker.matches_played < min_matches:
            skipped_few_matches += 1
            continue

        name = id_to_name.get(player_id, "").strip()
        if not name:
            skipped_no_name += 1
            continue

        # Surfaces
        surface_form = tracker.get_surface_form()
        surface_exp = tracker.get_surface_exp()

        # Surface win rates (raw, not rounded yet)
        surface_wr: dict[str, float] = {}
        for surf, results in tracker.surface_results.items():
            if results:
                wins = sum(1 for _, w in results if w)
                surface_wr[surf] = wins / len(results)

        # Fatigue relative to today
        fatigue = tracker.get_fatigue(reference_date)

        entry = {
            # Elo
            "overall": round(tracker.elo_overall, 2),
            "hard": round(float(tracker.elo_surface.get("hard", ELO_INIT)), 2),
            "clay": round(float(tracker.elo_surface.get("clay", ELO_INIT)), 2),
            "grass": round(float(tracker.elo_surface.get("grass", ELO_INIT)), 2),
            # Profile
            "rank": tracker.last_rank,
            "age": round(tracker.last_age, 1),
            "height": int(tracker.height) if tracker.height > 0 else 0,
            "hand": tracker.hand,
            # Activity
            "matches": tracker.matches_played,
            # Form
            "form5": round(tracker.get_form(n=5), 4),
            "form10": round(tracker.get_form(n=10), 4),
            "form20": round(tracker.get_form(n=20), 4),
            "surface_form": {k: round(v, 4) for k, v in surface_form.items()},
            "qa_form": round(tracker.get_quality_adjusted_form(10), 4),
            # Serve / return
            "serve": {k: round(v, 5) for k, v in tracker.get_avg_serve_stats(10).items()},
            "return": {k: round(v, 5) for k, v in tracker.get_avg_return_stats(10).items()},
            "hold_pct": round(tracker.get_hold_pct(10), 4),
            # Surface
            "surface_exp": surface_exp,
            "surface_wr": {k: round(v, 4) for k, v in surface_wr.items()},
            # Fatigue
            "fatigue": fatigue,
            # Meta
            "rank_momentum": round(tracker.get_rank_momentum(), 4),
            "entry": tracker.entry_type or "",
        }

        output[name] = entry

    logger.info(
        "Output: %d players. Skipped — few matches: %d, no name: %d",
        len(output), skipped_few_matches, skipped_no_name,
    )
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild elo_ratings.json with full player profile features from Sackmann CSVs.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Directory containing atp_matches_YYYY.csv and wta_matches_YYYY.csv files. "
            "Defaults to ../LXII Vegas/data/tennis/ (local) or /opt/tennis-dry-run/data/tennis/ (VPS)."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path. Defaults to tennis_dry_run/.tmp/tennis_data/elo_ratings.json",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2010,
        help="First year to load (default: 2010)",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=2026,
        help="Last year to load (default: 2026)",
    )
    parser.add_argument(
        "--min-matches",
        type=int,
        default=5,
        help="Minimum matches to include a player in output (default: 5)",
    )
    args = parser.parse_args()

    # Resolve data dir
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        # Try local first, fall back to VPS path
        local_path = Path(__file__).resolve().parent.parent.parent / "LXII Vegas" / "data" / "tennis"
        vps_path = Path("/opt/tennis-dry-run/data/tennis")
        if local_path.exists():
            data_dir = local_path
        elif vps_path.exists():
            data_dir = vps_path
        else:
            logger.error(
                "Could not find data directory. Tried:\n  %s\n  %s\n"
                "Pass --data-dir explicitly.",
                local_path, vps_path,
            )
            sys.exit(1)

    logger.info("Data directory: %s", data_dir)

    # Resolve output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(__file__).resolve().parent / ".tmp" / "tennis_data" / "elo_ratings.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Output path: %s", output_path)

    # Load
    matches = load_match_csvs(data_dir, args.start_year, args.end_year)
    if not matches:
        logger.error("No matches loaded — check data directory path.")
        sys.exit(1)

    # Process
    players, id_to_name = process_matches(matches)

    # Reference date for fatigue = today
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logger.info("Reference date for fatigue: %s", today.strftime("%Y-%m-%d"))

    # Build output
    output = build_output(players, id_to_name, today, args.min_matches)

    # Write
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    logger.info("Written: %s  (%d players)", output_path, len(output))

    # Quick sanity check
    sample_key = next(iter(output))
    sample = output[sample_key]
    logger.info(
        "Sample player '%s': overall=%.1f hard=%.1f clay=%.1f grass=%.1f "
        "rank=%d matches=%d form10=%.2f",
        sample_key,
        sample["overall"], sample["hard"], sample["clay"], sample["grass"],
        sample["rank"], sample["matches"], sample["form10"],
    )


if __name__ == "__main__":
    main()
