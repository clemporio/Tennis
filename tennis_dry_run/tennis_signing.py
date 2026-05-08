"""SX Bet v6.0 fill-order signer for tennis Phase 2 execution.

Endpoint: POST https://api.prod.sx.bet/orders/fill/v2
Schema reference: memory/reference_sxbet_signing.md

This module is intentionally self-contained — no dependency on the LXII Vegas
core/ package. The LTD soccer bot uses a different schema (maker /orders/new);
tennis uses the taker fill flow because picks are time-sensitive and we want
immediate fills against existing orderbook liquidity.
"""

import logging
import time
from typing import Literal, Optional

import requests

logger = logging.getLogger("tennis_signing")


# ── Constants (SX Rollup chain + USDC base token) ────────────────────────────

CHAIN_ID = 4162
USDC_TOKEN = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
USDC_DECIMALS = 10 ** 6
EIP712_VERIFYING_CONTRACT = "0x845a2Da2D70fEDe8474b1C8518200798c60aC364"

# percentageOdds = implied_prob * 10^20
ODDS_SCALE = 10 ** 20

# Ladder step: percentageOdds must be a multiple of 125 * 10^15
LADDER_STEP = 125 * (10 ** 15)

# 99% cap (per memory ref: percentageOdds bounds 1%..99%)
_MAX_PCT_ODDS = 99 * ODDS_SCALE // 100

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

DEFAULT_BASE_URL = "https://api.prod.sx.bet"


# ── Ladder snap ──────────────────────────────────────────────────────────────

def snap_to_ladder(
    percentage_odds: int,
    side: Literal["back", "lay"] = "back",
) -> int:
    """Snap a raw percentageOdds value to the SX Bet odds ladder.

    Backer side rounds DOWN (toward worse-for-backer) so the resulting order
    is more likely to find matching liquidity. Lay side rounds UP for the
    same reason on the opposite side.
    """
    if percentage_odds <= 0:
        return LADDER_STEP

    if side == "back":
        snapped = (percentage_odds // LADDER_STEP) * LADDER_STEP
    elif side == "lay":
        snapped = -((-percentage_odds) // LADDER_STEP) * LADDER_STEP
    else:
        raise ValueError(f"side must be 'back' or 'lay', got {side!r}")

    if snapped < LADDER_STEP:
        snapped = LADDER_STEP
    if snapped > _MAX_PCT_ODDS:
        snapped = (_MAX_PCT_ODDS // LADDER_STEP) * LADDER_STEP
    return snapped


# ── Conversions ──────────────────────────────────────────────────────────────

def decimal_to_pct_odds(decimal_odds: float, side: Literal["back", "lay"] = "back") -> int:
    """Decimal odds → ladder-snapped percentageOdds (taker perspective).

    For a backer at decimal odds X, the taker's implied probability is 1/X.
    """
    if decimal_odds is None or decimal_odds <= 1.0:
        raise ValueError(f"decimal_odds must be > 1.0, got {decimal_odds!r}")
    raw = int((1.0 / decimal_odds) * ODDS_SCALE)
    return snap_to_ladder(raw, side=side)


def usdc_to_atomic(usdc_amount: float) -> int:
    """USDC dollar amount → 6-decimal atomic units."""
    return int(round(usdc_amount * USDC_DECIMALS))


# ── Typed-data builder ───────────────────────────────────────────────────────

def build_typed_data(
    market_hash: str,
    stake_usd: float,
    taker_decimal_odds: float,
    is_taker_betting_outcome_one: bool,
    taker_address: str,
    fill_salt: int,
    odds_slippage: int = 1,
) -> dict:
    """Build the EIP-712 typed-data dict for a fill order.

    The schema (Details + FillObject) matches SX Bet v6.0's POST
    /orders/fill/v2 endpoint per memory/reference_sxbet_signing.md.

    Args:
        market_hash: 0x-prefixed bytes32 market identifier.
        stake_usd: Taker stake in USDC.
        taker_decimal_odds: Decimal odds at which the taker wants to be filled.
            Will be converted to taker-perspective percentageOdds and snapped
            to the ladder (rounded DOWN — slightly worse, more likely to fill).
        is_taker_betting_outcome_one: True if taker backs outcomeOne.
        taker_address: 0x-prefixed taker wallet address.
        fill_salt: 256-bit random integer (unique per order).
        odds_slippage: Number of ladder steps of acceptable slippage. Default 1.

    Returns:
        dict with `types`, `domain`, `primaryType`, and `message` keys, ready
        for `eth_account.Account.sign_typed_data`.
    """
    pct_odds_snapped = decimal_to_pct_odds(taker_decimal_odds, side="back")
    stake_wei = usdc_to_atomic(stake_usd)
    taker_implied = pct_odds_snapped / ODDS_SCALE
    taker_decimal_post_snap = 1.0 / taker_implied if taker_implied > 0 else 0.0
    worst_returning_usd = stake_usd * taker_decimal_post_snap

    market_hash_bytes = bytes.fromhex(market_hash.replace("0x", ""))

    domain = {
        "name": "SX Bet",
        "version": "6.0",
        "chainId": CHAIN_ID,
        "verifyingContract": EIP712_VERIFYING_CONTRACT,
    }

    types = {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Details": [
            {"name": "action", "type": "string"},
            {"name": "market", "type": "string"},
            {"name": "betting", "type": "string"},
            {"name": "stake", "type": "string"},
            {"name": "worstOdds", "type": "string"},
            {"name": "worstReturning", "type": "string"},
            {"name": "fills", "type": "FillObject"},
        ],
        "FillObject": [
            {"name": "stakeWei", "type": "string"},
            {"name": "marketHash", "type": "bytes32"},
            {"name": "baseToken", "type": "address"},
            {"name": "desiredOdds", "type": "string"},
            {"name": "oddsSlippage", "type": "uint256"},
            {"name": "isTakerBettingOutcomeOne", "type": "bool"},
            {"name": "fillSalt", "type": "uint256"},
            {"name": "beneficiary", "type": "address"},
            {"name": "beneficiaryType", "type": "uint8"},
            {"name": "cashOutTarget", "type": "bytes32"},
        ],
    }

    fill_object = {
        "stakeWei": str(stake_wei),
        "marketHash": market_hash_bytes,
        "baseToken": USDC_TOKEN,
        "desiredOdds": str(pct_odds_snapped),
        "oddsSlippage": odds_slippage,
        "isTakerBettingOutcomeOne": bool(is_taker_betting_outcome_one),
        "fillSalt": fill_salt,
        "beneficiary": ZERO_ADDRESS,
        "beneficiaryType": 0,
        "cashOutTarget": bytes.fromhex(ZERO_BYTES32.replace("0x", "")),
    }

    message = {
        "action": "N/A",
        "market": market_hash,
        "betting": "outcomeOne" if is_taker_betting_outcome_one else "outcomeTwo",
        "stake": f"${stake_usd:.2f}",
        "worstOdds": f"{taker_decimal_post_snap:.4f}",
        "worstReturning": f"${worst_returning_usd:.2f}",
        "fills": fill_object,
    }

    return {
        "types": types,
        "domain": domain,
        "primaryType": "Details",
        "message": message,
    }


# ── Signing ──────────────────────────────────────────────────────────────────

def sign_fill_order(typed_data: dict, private_key: str) -> str:
    """Sign typed data with eth_account, return 0x-prefixed hex signature."""
    from eth_account import Account

    sub_types = {k: v for k, v in typed_data["types"].items() if k != "EIP712Domain"}

    signed = Account.sign_typed_data(
        private_key,
        domain_data=typed_data["domain"],
        message_types=sub_types,
        message_data=typed_data["message"],
    )
    sig_hex = signed.signature.hex()
    return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex


# ── HTTP submission ──────────────────────────────────────────────────────────

def submit_fill_order(
    typed_data: dict,
    signature: str,
    taker_address: str,
    base_url: str = DEFAULT_BASE_URL,
    affiliate_address: str = ZERO_ADDRESS,
    timeout: int = 30,
) -> dict:
    """POST a signed fill order to /orders/fill/v2.

    Returns the parsed JSON response (status, data, message). Retries once on
    HTTP 429. Does NOT raise on non-2xx; the caller decides how to handle it.
    """
    fills = typed_data["message"]["fills"]
    market_hash_hex = fills["marketHash"]
    if isinstance(market_hash_hex, bytes):
        market_hash_hex = "0x" + market_hash_hex.hex()

    payload = {
        "stakeWei": fills["stakeWei"],
        "marketHash": market_hash_hex,
        "baseToken": fills["baseToken"],
        "desiredOdds": fills["desiredOdds"],
        "oddsSlippage": fills["oddsSlippage"],
        "isTakerBettingOutcomeOne": fills["isTakerBettingOutcomeOne"],
        "fillSalt": str(fills["fillSalt"]),
        "taker": taker_address,
        "takerSig": signature,
        "affiliateAddress": affiliate_address,
        "proxyTaker": False,
        "strictFillMode": False,
    }

    url = f"{base_url}/orders/fill/v2"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 429:
        time.sleep(2)
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    try:
        return {"http_status": response.status_code, "body": response.json()}
    except ValueError:
        return {"http_status": response.status_code, "body": {"raw": response.text[:500]}}


def canonical_payload_hash(typed_data: dict, signature: str) -> str:
    """Stable sha256 hash of the canonicalized fill request — for journal audit.

    Excludes the signature itself (we journal it separately if needed) and
    converts bytes fields to hex so the hash is reproducible across runs.
    """
    import hashlib
    import json

    fills = typed_data["message"]["fills"]
    market_hash_hex = fills["marketHash"]
    if isinstance(market_hash_hex, bytes):
        market_hash_hex = "0x" + market_hash_hex.hex()
    cash_out = fills["cashOutTarget"]
    if isinstance(cash_out, bytes):
        cash_out = "0x" + cash_out.hex()

    canonical = {
        "stakeWei": fills["stakeWei"],
        "marketHash": market_hash_hex,
        "desiredOdds": fills["desiredOdds"],
        "oddsSlippage": fills["oddsSlippage"],
        "isTakerBettingOutcomeOne": fills["isTakerBettingOutcomeOne"],
        "fillSalt": fills["fillSalt"],
        "cashOutTarget": cash_out,
    }
    encoded = json.dumps(canonical, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
