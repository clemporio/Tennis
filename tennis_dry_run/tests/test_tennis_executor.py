"""Tests for tennis_executor — execution layer for tennis_dry_run.

Covers:
  - dry_run mode never invokes the signer
  - live mode kill-switch blocks orders
  - daily-stake-cap and per-match-liability-cap circuit breakers
  - audit journal records submit + response events
  - settlement reconciliation logs the divergence metric (real vs paper P&L)
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tennis_executor as ex


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def dry_run_config(state_dir):
    return ex.ExecutorConfig(
        mode="dry_run",
        base_stake_usd=25.0,
        max_daily_stake_usd=100.0,
        max_match_liability_usd=50.0,
        kill_switch_path=str(state_dir / "KILL"),
        orders_journal_path=str(state_dir / "orders.jsonl"),
        wallet_address="0xdeadbeef00000000000000000000000000000000",
        private_key="0x" + "11" * 32,
    )


@pytest.fixture
def live_config(state_dir):
    return ex.ExecutorConfig(
        mode="live",
        base_stake_usd=25.0,
        max_daily_stake_usd=100.0,
        max_match_liability_usd=50.0,
        kill_switch_path=str(state_dir / "KILL"),
        orders_journal_path=str(state_dir / "orders.jsonl"),
        wallet_address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        private_key="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    )


def make_pick(**overrides):
    base = {
        "pick_id": "0x" + "ab" * 32,
        "pick": "Test Player",
        "opponent": "Other Player",
        "league": "Test Open",
        "surface": "hard",
        "round": "R64",
        "model_prob": 0.85,
        "fair_odds": 1.18,
        "sxbet_odds": 1.85,
        "sxbet_available_usd": 100.0,
        "implied_prob": 0.541,
        "edge": 0.31,
        "market_hash": "0x" + "ab" * 32,
        "is_pick_outcome_one": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


# ── Dry-run mode ──────────────────────────────────────────────────────────────

class TestDryRunMode:

    def test_dry_run_never_calls_signer(self, dry_run_config):
        executor = ex.TennisExecutor(dry_run_config)
        with patch.object(ex, "sign_fill_order") as mock_sign, \
             patch.object(ex, "submit_fill_order") as mock_submit:
            result = executor.place_order(make_pick())

        assert mock_sign.call_count == 0
        assert mock_submit.call_count == 0
        assert result.status == "dry_run_recorded"
        assert result.mode == "dry_run"

    def test_dry_run_returns_paper_trade_entry(self, dry_run_config):
        executor = ex.TennisExecutor(dry_run_config)
        result = executor.place_order(make_pick(sxbet_odds=2.0))
        entry = result.trade_entry
        assert entry["mode"] == "dry_run"
        assert entry["stake"] == 25.0
        assert entry["sxbet_odds"] == 2.0
        assert "order_id" not in entry  # no live order placed

    def test_dry_run_does_not_journal_orders(self, dry_run_config, state_dir):
        executor = ex.TennisExecutor(dry_run_config)
        executor.place_order(make_pick())
        # No live submit/response → no orders.jsonl writes
        assert not (state_dir / "orders.jsonl").exists()


# ── Kill switch ───────────────────────────────────────────────────────────────

class TestKillSwitch:

    def test_live_order_blocked_when_kill_file_present(self, live_config, state_dir):
        Path(live_config.kill_switch_path).touch()
        executor = ex.TennisExecutor(live_config)
        with patch.object(ex, "sign_fill_order") as mock_sign:
            result = executor.place_order(make_pick())

        assert result.status == "blocked"
        assert "kill_switch" in result.block_reason
        assert mock_sign.call_count == 0

    def test_dry_run_unaffected_by_kill_switch(self, dry_run_config, state_dir):
        Path(dry_run_config.kill_switch_path).touch()
        executor = ex.TennisExecutor(dry_run_config)
        result = executor.place_order(make_pick())
        # Kill switch is for live orders only — dry runs continue
        assert result.status == "dry_run_recorded"


# ── Risk caps ────────────────────────────────────────────────────────────────

class TestRiskCaps:

    def test_daily_stake_cap_blocks_live_order(self, live_config):
        executor = ex.TennisExecutor(live_config)
        executor.set_today_live_stake(99.0)  # one more $25 stake exceeds $100 cap
        with patch.object(ex, "sign_fill_order") as mock_sign:
            result = executor.place_order(make_pick())
        assert result.status == "blocked"
        assert "daily_stake_cap" in result.block_reason
        assert mock_sign.call_count == 0

    def test_per_match_liability_cap_blocks_live_order(self, live_config):
        executor = ex.TennisExecutor(live_config)
        # liability = stake * (odds - 1). With $25 stake, odds 5.0 → $100 liability,
        # which exceeds the $50 cap.
        with patch.object(ex, "sign_fill_order") as mock_sign:
            result = executor.place_order(make_pick(sxbet_odds=5.0))
        assert result.status == "blocked"
        assert "match_liability_cap" in result.block_reason
        assert mock_sign.call_count == 0

    def test_caps_not_enforced_in_dry_run_mode(self, dry_run_config):
        executor = ex.TennisExecutor(dry_run_config)
        executor.set_today_live_stake(99999.0)
        result = executor.place_order(make_pick(sxbet_odds=10.0))
        # Dry run records anyway — caps only matter for live writes
        assert result.status == "dry_run_recorded"


# ── Journaling ───────────────────────────────────────────────────────────────

class TestAuditJournal:

    def test_live_order_writes_submit_and_response_lines(self, live_config, state_dir):
        executor = ex.TennisExecutor(live_config)
        fake_response = {
            "http_status": 200,
            "body": {
                "status": "success",
                "data": {
                    "orderHash": "0x" + "cd" * 32,
                    "fillAmount": str(25 * 10 ** 6),
                    "filledOdds": "54054054054054050000",
                },
            },
        }
        with patch.object(ex, "sign_fill_order", return_value="0x" + "ee" * 65), \
             patch.object(ex, "submit_fill_order", return_value=fake_response):
            executor.place_order(make_pick())

        lines = (state_dir / "orders.jsonl").read_text().strip().splitlines()
        assert len(lines) >= 2

        import json
        events = [json.loads(line)["event"] for line in lines]
        assert "submit" in events
        assert "response" in events

    def test_journal_submit_includes_payload_hash(self, live_config, state_dir):
        executor = ex.TennisExecutor(live_config)
        fake_response = {
            "http_status": 200,
            "body": {"status": "success", "data": {"orderHash": "0x" + "cd" * 32}},
        }
        with patch.object(ex, "sign_fill_order", return_value="0x" + "ee" * 65), \
             patch.object(ex, "submit_fill_order", return_value=fake_response):
            executor.place_order(make_pick())

        import json
        lines = (state_dir / "orders.jsonl").read_text().strip().splitlines()
        submit = next(json.loads(l) for l in lines if json.loads(l)["event"] == "submit")
        assert "payload_hash" in submit
        # Hash must be deterministic (sha256 hex)
        assert len(submit["payload_hash"]) == 64

    def test_private_key_never_appears_in_journal(self, live_config, state_dir):
        executor = ex.TennisExecutor(live_config)
        fake_response = {
            "http_status": 200,
            "body": {"status": "success", "data": {"orderHash": "0x" + "cd" * 32}},
        }
        with patch.object(ex, "sign_fill_order", return_value="0x" + "ee" * 65), \
             patch.object(ex, "submit_fill_order", return_value=fake_response):
            executor.place_order(make_pick())
        contents = (state_dir / "orders.jsonl").read_text()
        assert live_config.private_key not in contents


# ── Settlement reconciliation ─────────────────────────────────────────────────

class TestReconcile:

    def test_dry_run_pick_paper_pnl_unchanged(self, dry_run_config):
        executor = ex.TennisExecutor(dry_run_config)
        pick = {
            "pick_id": "p1", "pick": "Player A", "stake": 25.0, "sxbet_odds": 2.0,
            "mode": "dry_run",
        }
        settlement = executor.reconcile_pick(pick, won=True)
        assert settlement["mode"] == "dry_run"
        assert settlement["pnl"] == 25.0  # 25 * (2.0 - 1)
        assert "divergence_usd" not in settlement

    def test_live_pick_logs_divergence_against_paper(self, live_config, state_dir):
        executor = ex.TennisExecutor(live_config)
        # Recorded as live with desired odds 2.0 but actually filled at 1.85
        pick = {
            "pick_id": "p2", "pick": "Player B", "stake": 25.0,
            "sxbet_odds": 2.0,                   # what we'd have used for paper
            "filled_decimal_odds": 1.85,         # actual fill
            "filled_stake_usd": 25.0,
            "mode": "live",
            "order_id": "0x" + "cd" * 32,
        }
        settlement = executor.reconcile_pick(pick, won=True)
        assert settlement["mode"] == "live"
        assert settlement["pnl"] == pytest.approx(25.0 * (1.85 - 1))   # real pnl
        assert "divergence_usd" in settlement
        assert settlement["divergence_usd"] == pytest.approx(
            settlement["pnl"] - 25.0 * (2.0 - 1)
        )

        # And it's journaled
        import json
        lines = (state_dir / "orders.jsonl").read_text().strip().splitlines()
        events = [json.loads(line) for line in lines]
        settled_events = [e for e in events if e["event"] == "settled"]
        assert len(settled_events) == 1
        assert "divergence_usd" in settled_events[0]
