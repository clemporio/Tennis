"""run_scan must reject Challenger / qualifying / ITF markets.

Mirrors the identifier's EXCLUDED_LEAGUE_SUBSTRINGS rule.

Interim mitigation per Task 12 spec finding #2. Deleted in Task H when
run_scan becomes a no-op.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("league", [
    "ATP Challenger Madrid",
    "WTA 125 Challenger",
    "ATP Rome Qualifying",
    "Davis Cup Qualif.",
    "ITF M25 Heraklion",
    "ATP Rome Q1",
])
def test_run_scan_rejects_excluded_leagues(league, monkeypatch, tmp_path):
    """A market whose league matches the exclusion list must be skipped by run_scan.

    Stubs every downstream gate so a market would pass without the league
    filter. The executor raises if `place_order` is invoked — a strong
    signal the league check did not reject.
    """
    import tennis_dry_run as tdr
    import tennis_sxbet

    monkeypatch.setattr(tdr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tdr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(tdr, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(tdr, "DAILY_FILE", tmp_path / "settlements.jsonl")
    monkeypatch.setattr(tdr, "SKIPPED_FILE", tmp_path / "skipped.jsonl")
    monkeypatch.setattr(tdr, "ELO_FILE", tmp_path / "nonexistent_elo.json")

    market_hash = "0xdeadbeef" + "00" * 28

    class _Sxbet:
        def get_all_tennis_markets(self):
            return [{
                "market_hash": market_hash,
                "player_a": "Player A", "player_b": "Player B",
                "league": league,
            }]
        def get_best_back_odds(self, mh, pick_name, outcome_one_name):
            return {"decimal_odds": 1.50, "available_usd": 200.0}
    monkeypatch.setattr(tennis_sxbet, "TennisSXBet", _Sxbet)
    monkeypatch.setattr(tdr, "scrape_scheduled_matches", lambda: [])
    monkeypatch.setattr(tdr, "_find_player_elo", lambda name, elo_data: {"overall": 1700.0})
    monkeypatch.setattr(tdr, "_build_model_input", lambda elo_entry, surface: {"pa_elo": 1700.0})

    class _Predictor:
        MIN_CONFIDENCE = 0.0
        def load(self): return True
        def predict_match(self, **kw): return {"prob_a": 0.9, "prob_b": 0.1}
    monkeypatch.setattr(tdr, "TennisModelPredictor", _Predictor)

    class _Exec:
        def place_order(self, pick):
            raise AssertionError(
                f"place_order called for excluded league {league!r}; "
                f"league filter is not enforced in run_scan"
            )

    state = {
        "balance": 500.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        "open_picks": {}, "today_bets": 0, "today_date": "2026-05-14",
    }
    new_state = tdr.run_scan(state, _Exec())

    assert new_state["open_picks"] == {}, \
        f"Challenger market {league!r} was not excluded — open_picks: {new_state['open_picks']}"
    journal_path = tmp_path / "trades.jsonl"
    if journal_path.exists():
        journal = journal_path.read_text(encoding="utf-8")
        assert '"type": "open"' not in journal, \
            f"journal contains open row for excluded league {league!r}: {journal}"
