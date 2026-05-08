"""
Tennis Model Predictor — inference pipeline for live match predictions.

Loads the trained LightGBM model and computes win probabilities for
upcoming matches. Designed to be called from the tennis scanner with
pre-computed player data.

Only returns picks at 80%+ model confidence (87.1% backtested accuracy).
"""

import json
import logging
from pathlib import Path
from collections import defaultdict
from typing import Optional

import numpy as np
import lightgbm as lgb
import joblib

logger = logging.getLogger("signals.tennis_model")

MODEL_DIR = Path(__file__).resolve().parent / "saved"


class TennisModelPredictor:
    """Predict match outcomes using the trained LightGBM model."""

    MIN_CONFIDENCE = 0.80  # Only output 80%+ picks

    def __init__(self):
        self.model = None
        self.calibrator = None
        self.feature_names = None
        self.metadata = None
        self._loaded = False

    def load(self) -> bool:
        """Load model from disk."""
        model_path = MODEL_DIR / "tennis_lgbm.txt"
        cal_path = MODEL_DIR / "tennis_calibrator.joblib"
        meta_path = MODEL_DIR / "metadata.json"

        if not model_path.exists():
            logger.warning(f"Tennis model not found at {model_path}")
            return False

        try:
            self.model = lgb.Booster(model_file=str(model_path))
            self.calibrator = joblib.load(cal_path) if cal_path.exists() else None
            self.metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            self.feature_names = self.metadata.get("feature_names", [])
            self._loaded = True
            ver = self.metadata.get('version', '?')
            ver_str = ver if str(ver).startswith('v') else f"v{ver}"
            logger.info(f"Tennis model loaded: {len(self.feature_names)} features, {ver_str}")
            return True
        except Exception as e:
            logger.error(f"Failed to load tennis model: {e}")
            return False

    def predict_match(
        self,
        # Player A data
        pa_elo: float, pa_surface_elo: float, pa_rank: int, pa_age: float,
        pa_height: float, pa_hand: str, pa_matches_played: int,
        pa_form5: float, pa_form10: float, pa_form20: float,
        pa_surface_form: float, pa_qa_form: float,
        pa_serve: dict, pa_return: dict, pa_hold_pct: float,
        pa_surface_exp: int, pa_surface_wr: float,
        pa_fatigue: dict, pa_rank_momentum: float,
        pa_entry: str,
        # Player B data (same structure)
        pb_elo: float, pb_surface_elo: float, pb_rank: int, pb_age: float,
        pb_height: float, pb_hand: str, pb_matches_played: int,
        pb_form5: float, pb_form10: float, pb_form20: float,
        pb_surface_form: float, pb_qa_form: float,
        pb_serve: dict, pb_return: dict, pb_hold_pct: float,
        pb_surface_exp: int, pb_surface_wr: float,
        pb_fatigue: dict, pb_rank_momentum: float,
        pb_entry: str,
        # Match context
        surface: str = "hard",
    ) -> Optional[dict]:
        """
        Predict match outcome.

        Returns dict with player_a_prob, player_b_prob, pick, confidence
        or None if model not loaded or below confidence threshold.
        """
        if not self._loaded:
            if not self.load():
                return None

        # Build feature vector matching training order
        features = self._build_features(
            pa_elo, pa_surface_elo, pa_rank, pa_age, pa_height, pa_hand,
            pa_matches_played, pa_form5, pa_form10, pa_form20,
            pa_surface_form, pa_qa_form, pa_serve, pa_return, pa_hold_pct,
            pa_surface_exp, pa_surface_wr, pa_fatigue, pa_rank_momentum, pa_entry,
            pb_elo, pb_surface_elo, pb_rank, pb_age, pb_height, pb_hand,
            pb_matches_played, pb_form5, pb_form10, pb_form20,
            pb_surface_form, pb_qa_form, pb_serve, pb_return, pb_hold_pct,
            pb_surface_exp, pb_surface_wr, pb_fatigue, pb_rank_momentum, pb_entry,
            surface,
        )

        X = np.array([features])
        raw = self.model.predict(X)

        if self.calibrator:
            prob_a = self.calibrator.predict_proba(raw.reshape(-1, 1))[0, 1]
        else:
            prob_a = 1.0 / (1.0 + np.exp(-raw[0]))

        prob_b = 1.0 - prob_a

        # Determine pick and confidence
        best_prob = max(prob_a, prob_b)
        pick = "A" if prob_a >= prob_b else "B"

        if best_prob < self.MIN_CONFIDENCE:
            return None  # Below threshold

        return {
            "prob_a": round(float(prob_a), 4),
            "prob_b": round(float(prob_b), 4),
            "pick": pick,
            "confidence": round(float(best_prob), 4),
        }

    def _build_features(
        self,
        pa_elo, pa_surface_elo, pa_rank, pa_age, pa_height, pa_hand,
        pa_matches_played, pa_form5, pa_form10, pa_form20,
        pa_surface_form, pa_qa_form, pa_serve, pa_return, pa_hold_pct,
        pa_surface_exp, pa_surface_wr, pa_fatigue, pa_rank_momentum, pa_entry,
        pb_elo, pb_surface_elo, pb_rank, pb_age, pb_height, pb_hand,
        pb_matches_played, pb_form5, pb_form10, pb_form20,
        pb_surface_form, pb_qa_form, pb_serve, pb_return, pb_hold_pct,
        pb_surface_exp, pb_surface_wr, pb_fatigue, pb_rank_momentum, pb_entry,
        surface,
    ) -> list[float]:
        """Build the 44-feature vector in the exact order the model expects."""

        elo_diff = pa_elo - pb_elo
        surface_elo_diff = pa_surface_elo - pb_surface_elo
        rank_diff = (pb_rank - pa_rank) / 100.0
        age_diff = pa_age - pb_age
        height_diff = (pa_height - pb_height) / 10.0 if pa_height > 0 and pb_height > 0 else 0
        exp_diff = (pa_matches_played - pb_matches_played) / 100.0

        hand_matchup = 0
        if pa_hand and pb_hand:
            if pa_hand == "R" and pb_hand == "L":
                hand_matchup = 1
            elif pa_hand == "L" and pb_hand == "R":
                hand_matchup = -1

        pa_rank_implied = 1.0 / (1.0 + 10 ** ((pa_rank - pb_rank) / 250.0)) if pa_rank > 0 and pb_rank > 0 else 0.5

        rest_diff = (pa_fatigue.get("days_since_last", 7) - pb_fatigue.get("days_since_last", 7)) / 7.0

        # Interaction features
        ix_elo_surface = elo_diff * (pa_surface_wr - pb_surface_wr)
        ix_hold_bp = pa_hold_pct * pa_return.get("bp_converted_pct", 0) - pb_hold_pct * pb_return.get("bp_converted_pct", 0)

        # Map feature names to values
        feature_map = {
            "elo_diff": round(elo_diff, 1),
            "surface_elo_diff": round(surface_elo_diff, 1),
            "rank_diff": round(rank_diff, 2),
            "pa_rank_implied_prob": round(pa_rank_implied, 4),
            "rank_momentum_diff": round(pa_rank_momentum - pb_rank_momentum, 3),
            "age_diff": round(age_diff / 10.0, 3),
            "height_diff": round(height_diff, 2),
            "hand_matchup": hand_matchup,
            "exp_diff": round(exp_diff, 2),
            "form5_diff": round(pa_form5 - pb_form5, 3),
            "form10_diff": round(pa_form10 - pb_form10, 3),
            "form20_diff": round(pa_form20 - pb_form20, 3),
            "surface_form_diff": round(pa_surface_form - pb_surface_form, 3),
            "pa_form5": round(pa_form5, 3),
            "pb_form5": round(pb_form5, 3),
            "qa_form_diff": round(pa_qa_form - pb_qa_form, 3),
            "ace_rate_diff": round(pa_serve.get("ace_rate", 0) - pb_serve.get("ace_rate", 0), 4),
            "df_rate_diff": round(pa_serve.get("df_rate", 0) - pb_serve.get("df_rate", 0), 4),
            "first_in_diff": round(pa_serve.get("first_in_pct", 0) - pb_serve.get("first_in_pct", 0), 4),
            "first_won_diff": round(pa_serve.get("first_won_pct", 0) - pb_serve.get("first_won_pct", 0), 4),
            "second_won_diff": round(pa_serve.get("second_won_pct", 0) - pb_serve.get("second_won_pct", 0), 4),
            "bp_saved_diff": round(pa_serve.get("bp_saved_pct", 0) - pb_serve.get("bp_saved_pct", 0), 4),
            "bp_converted_diff": round(pa_return.get("bp_converted_pct", 0) - pb_return.get("bp_converted_pct", 0), 4),
            "return_pts_diff": round(pa_return.get("return_pts_won_pct", 0) - pb_return.get("return_pts_won_pct", 0), 4),
            "hold_pct_diff": round(pa_hold_pct - pb_hold_pct, 3),
            "surface_wr_diff": round(pa_surface_wr - pb_surface_wr, 3),
            "pa_surface_exp": min(pa_surface_exp / 50.0, 1.0),
            "pb_surface_exp": min(pb_surface_exp / 50.0, 1.0),
            "fatigue_matches7d_diff": pa_fatigue.get("matches_7d", 0) - pb_fatigue.get("matches_7d", 0),
            "fatigue_matches14d_diff": pa_fatigue.get("matches_14d", 0) - pb_fatigue.get("matches_14d", 0),
            "fatigue_sets14d_diff": (pa_fatigue.get("sets_14d", 0) - pb_fatigue.get("sets_14d", 0)) / 10.0,
            "pa_days_since_last": min(pa_fatigue.get("days_since_last", 7) / 30.0, 1.0),
            "pb_days_since_last": min(pb_fatigue.get("days_since_last", 7) / 30.0, 1.0),
            "rest_diff_norm": round(rest_diff, 3),
            "prev_round_minutes_diff": 0.0,  # Not available at inference time
            "prev_round_sets_diff": 0.0,
            "pa_is_qualifier": 1 if pa_entry in ("Q", "q") else 0,
            "pb_is_qualifier": 1 if pb_entry in ("Q", "q") else 0,
            "pa_is_wildcard": 1 if pa_entry in ("WC", "wc") else 0,
            "pb_is_wildcard": 1 if pb_entry in ("WC", "wc") else 0,
            "qualifier_diff": (1 if pa_entry in ("Q", "q") else 0) - (1 if pb_entry in ("Q", "q") else 0),
            "age_surface_interaction": age_diff * (1 if surface == "clay" else (-0.5 if surface == "grass" else 0)),
            "ix_elo_diff_x_surface_wr_diff": round(ix_elo_surface, 2),
            "ix_hold_pct_diff_x_bp_converted_diff": round(ix_hold_bp, 4),
        }

        # Return in the exact order the model expects
        return [feature_map.get(f, 0.0) for f in self.feature_names]
