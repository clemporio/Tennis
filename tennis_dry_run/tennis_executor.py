"""Tennis execution layer — dry-run paper recording or live SX Bet fill orders.

The main loop in tennis_dry_run.py composes a pick (model probability, fair
odds, observed best-back odds, etc.) and hands it to TennisExecutor.place_order.
The executor decides whether to record a paper trade (dry_run mode) or sign
and submit a real /orders/fill/v2 request (live mode), with risk caps and a
file-based kill switch enforced before any signing.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from tennis_signing import (
    LADDER_STEP,
    ODDS_SCALE,
    build_typed_data,
    canonical_payload_hash,
    decimal_to_pct_odds,
    sign_fill_order,
    submit_fill_order,
)

log = logging.getLogger("tennis_executor")


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class ExecutorConfig:
    mode: Literal["dry_run", "live"]
    base_stake_usd: float
    max_daily_stake_usd: float
    max_match_liability_usd: float
    kill_switch_path: str
    orders_journal_path: str
    wallet_address: str
    private_key: str
    sxbet_base_url: str = "https://api.prod.sx.bet"

    @classmethod
    def from_env(cls, defaults_dir: Path) -> "ExecutorConfig":
        return cls(
            mode=os.getenv("TENNIS_MODE", "dry_run").lower(),
            base_stake_usd=float(os.getenv("TENNIS_BASE_STAKE_USD", "25.0")),
            max_daily_stake_usd=float(os.getenv("MAX_DAILY_STAKE_USD", "100.0")),
            max_match_liability_usd=float(os.getenv("MAX_MATCH_LIABILITY_USD", "50.0")),
            kill_switch_path=os.getenv(
                "KILL_SWITCH_PATH", str(defaults_dir / "KILL")
            ),
            orders_journal_path=os.getenv(
                "ORDERS_JOURNAL_PATH", str(defaults_dir / "orders.jsonl")
            ),
            wallet_address=os.getenv("SXBET_WALLET_ADDRESS", ""),
            private_key=os.getenv("SXBET_PRIVATE_KEY", ""),
            sxbet_base_url=os.getenv("SXBET_API_BASE", "https://api.prod.sx.bet"),
        )


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    """Returned from `place_order`. The main loop only ever needs to look at
    `status` and `trade_entry`. The other fields are populated for the live
    path so settlement can reconcile against the actual fill."""

    status: Literal[
        "dry_run_recorded",
        "live_filled",
        "live_partial",
        "live_failed",
        "blocked",
    ]
    mode: Literal["dry_run", "live"]
    trade_entry: dict
    block_reason: Optional[str] = None
    order_id: Optional[str] = None
    filled_stake_usd: Optional[float] = None
    filled_decimal_odds: Optional[float] = None


# ── Executor ─────────────────────────────────────────────────────────────────

class TennisExecutor:

    def __init__(self, config: ExecutorConfig):
        self.config = config
        self._today_live_stake: float = 0.0
        self._today_date: str = self._today_str()

        Path(self.config.orders_journal_path).parent.mkdir(parents=True, exist_ok=True)

    # -- public surface --

    def kill_switch_active(self) -> bool:
        return os.path.exists(self.config.kill_switch_path)

    def set_today_live_stake(self, amount: float) -> None:
        """Hydrate the daily accumulator from persisted state at startup."""
        self._today_live_stake = amount
        self._today_date = self._today_str()

    def place_order(self, pick: dict) -> OrderResult:
        self._roll_daily_accumulator()

        if self.config.mode == "dry_run":
            return self._record_dry_run(pick)

        block_reason = self._check_live_caps(pick)
        if block_reason is not None:
            return OrderResult(
                status="blocked",
                mode="live",
                trade_entry={},
                block_reason=block_reason,
            )

        return self._submit_live(pick)

    def reconcile_pick(self, pick: dict, won: bool) -> dict:
        """Compute a settlement record. For live picks, also log the
        divergence between real and paper P&L to the orders journal."""
        mode = pick.get("mode", "dry_run")
        stake = float(pick.get("stake", self.config.base_stake_usd))
        sxbet_odds = float(pick.get("sxbet_odds", 0.0))
        paper_pnl = stake * (sxbet_odds - 1) if won else -stake

        if mode == "live":
            filled_stake = float(pick.get("filled_stake_usd", stake))
            filled_odds = float(pick.get("filled_decimal_odds", sxbet_odds))
            real_pnl = filled_stake * (filled_odds - 1) if won else -filled_stake
            settlement = {
                "mode": "live",
                "outcome": "win" if won else "loss",
                "pnl": round(real_pnl, 4),
                "paper_pnl": round(paper_pnl, 4),
                "divergence_usd": round(real_pnl - paper_pnl, 4),
                "filled_stake_usd": filled_stake,
                "filled_decimal_odds": filled_odds,
                "order_id": pick.get("order_id"),
            }
            self._journal_event({
                "event": "settled",
                "pick_id": pick.get("pick_id"),
                **settlement,
            })
            return settlement

        return {
            "mode": "dry_run",
            "outcome": "win" if won else "loss",
            "pnl": round(paper_pnl, 4),
        }

    # -- dry-run path --

    def _record_dry_run(self, pick: dict) -> OrderResult:
        entry = self._base_trade_entry(pick, mode="dry_run")
        return OrderResult(
            status="dry_run_recorded",
            mode="dry_run",
            trade_entry=entry,
        )

    # -- live path --

    def _check_live_caps(self, pick: dict) -> Optional[str]:
        if self.kill_switch_active():
            return f"kill_switch:{self.config.kill_switch_path}"

        stake = self.config.base_stake_usd
        if self._today_live_stake + stake > self.config.max_daily_stake_usd:
            return (
                f"daily_stake_cap: today={self._today_live_stake:.2f} "
                f"+ stake={stake:.2f} > cap={self.config.max_daily_stake_usd:.2f}"
            )

        sxbet_odds = float(pick.get("sxbet_odds", 0.0))
        liability = stake * (sxbet_odds - 1)
        if liability > self.config.max_match_liability_usd:
            return (
                f"match_liability_cap: liability={liability:.2f} "
                f"> cap={self.config.max_match_liability_usd:.2f}"
            )

        if not self.config.wallet_address or not self.config.private_key:
            return "missing_wallet_credentials"

        return None

    def _submit_live(self, pick: dict) -> OrderResult:
        market_hash = pick["market_hash"]
        is_outcome_one = bool(pick.get("is_pick_outcome_one", True))
        stake = self.config.base_stake_usd
        decimal_odds = float(pick["sxbet_odds"])

        fill_salt = secrets.randbits(256)

        typed_data = build_typed_data(
            market_hash=market_hash,
            stake_usd=stake,
            taker_decimal_odds=decimal_odds,
            is_taker_betting_outcome_one=is_outcome_one,
            taker_address=self.config.wallet_address,
            fill_salt=fill_salt,
        )

        signature = sign_fill_order(typed_data, self.config.private_key)
        payload_hash = canonical_payload_hash(typed_data, signature)
        submit_ts = self._now_iso()

        self._journal_event({
            "event": "submit",
            "pick_id": pick.get("pick_id"),
            "market_hash": market_hash,
            "stake": stake,
            "desired_decimal_odds": decimal_odds,
            "is_taker_betting_outcome_one": is_outcome_one,
            "fill_salt": str(fill_salt),
            "payload_hash": payload_hash,
            "submit_ts": submit_ts,
        })

        try:
            response = submit_fill_order(
                typed_data=typed_data,
                signature=signature,
                taker_address=self.config.wallet_address,
                base_url=self.config.sxbet_base_url,
            )
        except Exception as exc:
            log.exception("submit_fill_order raised: %s", exc)
            response = {"http_status": 0, "body": {"error": str(exc)}}

        response_ts = self._now_iso()
        self._journal_event({
            "event": "response",
            "pick_id": pick.get("pick_id"),
            "submit_ts": submit_ts,
            "response_ts": response_ts,
            "http_status": response.get("http_status"),
            "body": response.get("body"),
        })

        return self._build_live_result(pick, response, stake, decimal_odds,
                                       submit_ts, response_ts)

    def _build_live_result(
        self,
        pick: dict,
        response: dict,
        stake: float,
        desired_odds: float,
        submit_ts: str,
        response_ts: str,
    ) -> OrderResult:
        body = response.get("body") or {}
        http_status = response.get("http_status")
        success = http_status == 200 and body.get("status") == "success"

        if not success:
            err = body.get("message") or body.get("error") or f"http_{http_status}"
            entry = self._base_trade_entry(pick, mode="live")
            entry.update({
                "submit_ts": submit_ts,
                "response_ts": response_ts,
                "fill_status": "failed",
                "error": str(err)[:500],
            })
            return OrderResult(
                status="live_failed", mode="live", trade_entry=entry,
                block_reason=str(err)[:200],
            )

        data = body.get("data", {}) or {}
        order_id = data.get("orderHash") or data.get("orderId") or ""

        fill_amount_atomic = int(data.get("fillAmount", 0) or 0)
        filled_stake_usd = fill_amount_atomic / 10 ** 6 if fill_amount_atomic else stake

        filled_pct_str = data.get("filledOdds")
        if filled_pct_str:
            try:
                filled_pct = int(filled_pct_str)
                implied = filled_pct / ODDS_SCALE
                filled_decimal = round(1.0 / implied, 4) if implied > 0 else desired_odds
            except (TypeError, ValueError):
                filled_decimal = desired_odds
        else:
            filled_decimal = desired_odds

        # Update daily accumulator
        self._today_live_stake += filled_stake_usd

        partial = filled_stake_usd < stake - 1e-6
        status = "live_partial" if partial else "live_filled"

        entry = self._base_trade_entry(pick, mode="live")
        entry.update({
            "submit_ts": submit_ts,
            "response_ts": response_ts,
            "fill_status": status,
            "order_id": order_id,
            "filled_stake_usd": round(filled_stake_usd, 4),
            "filled_decimal_odds": filled_decimal,
        })

        return OrderResult(
            status=status, mode="live", trade_entry=entry,
            order_id=order_id,
            filled_stake_usd=filled_stake_usd,
            filled_decimal_odds=filled_decimal,
        )

    # -- helpers --

    def _base_trade_entry(self, pick: dict, mode: str) -> dict:
        stake = self.config.base_stake_usd
        return {
            "type": "open",
            "mode": mode,
            "pick_id": pick.get("pick_id"),
            "pick": pick.get("pick"),
            "opponent": pick.get("opponent"),
            "league": pick.get("league"),
            "surface": pick.get("surface"),
            "round": pick.get("round"),
            "model_prob": pick.get("model_prob"),
            "fair_odds": pick.get("fair_odds"),
            "sxbet_odds": pick.get("sxbet_odds"),
            "sxbet_available_usd": pick.get("sxbet_available_usd"),
            "implied_prob": pick.get("implied_prob"),
            "edge": pick.get("edge"),
            "stake": stake,
            "market_hash": pick.get("market_hash"),
            "ts": pick.get("ts") or self._now_iso(),
        }

    def _journal_event(self, event: dict) -> None:
        record = {"ts": self._now_iso(), "mode": self.config.mode, **event}
        with open(self.config.orders_journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _roll_daily_accumulator(self) -> None:
        today = self._today_str()
        if today != self._today_date:
            self._today_date = today
            self._today_live_stake = 0.0

    @staticmethod
    def _today_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
