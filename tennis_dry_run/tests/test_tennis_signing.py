"""Tests for tennis_signing — EIP-712 fill-order signer for SX Bet v6.0.

Schema reference: memory/reference_sxbet_signing.md
Endpoint: POST https://api.prod.sx.bet/orders/fill/v2

These tests cover the FILL (taker) flow only. The MAKE (maker) flow exists
elsewhere in the codebase for the LTD bot but is not used by tennis Phase 2.
"""

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

import tennis_signing as signing


# Anvil/Hardhat default account #0 — public test key, do not use for real funds.
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

# Fixed inputs for golden-vector tests (chosen arbitrarily but locked)
FIXED_MARKET_HASH = "0x" + "ab" * 32
FIXED_STAKE_USD = 25.0
FIXED_TAKER_DECIMAL_ODDS = 1.85
FIXED_FILL_SALT = 12345678901234567890


# ── Ladder snap ───────────────────────────────────────────────────────────────

class TestLadderSnap:

    def test_step_size_is_125e15(self):
        assert signing.LADDER_STEP == 125 * (10 ** 15)

    def test_back_side_rounds_down(self):
        # raw value = step * 4 + half a step
        raw = signing.LADDER_STEP * 4 + signing.LADDER_STEP // 2
        snapped = signing.snap_to_ladder(raw, side="back")
        assert snapped == signing.LADDER_STEP * 4

    def test_back_side_exact_multiple_unchanged(self):
        raw = signing.LADDER_STEP * 7
        assert signing.snap_to_ladder(raw, side="back") == raw

    def test_back_side_below_minimum_clamps_to_step(self):
        snapped = signing.snap_to_ladder(0, side="back")
        assert snapped == signing.LADDER_STEP

    def test_back_side_above_max_clamps(self):
        # 99% of 10^20 is the cap
        too_high = 100 * (10 ** 20)
        snapped = signing.snap_to_ladder(too_high, side="back")
        assert snapped <= 99 * (10 ** 20) // 100


# ── Typed-data structure ──────────────────────────────────────────────────────

class TestTypedDataStructure:

    def test_domain_matches_v6_schema(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        assert td["domain"]["name"] == "SX Bet"
        assert td["domain"]["version"] == "6.0"
        assert td["domain"]["chainId"] == 4162
        assert td["domain"]["verifyingContract"] == "0x845a2Da2D70fEDe8474b1C8518200798c60aC364"

    def test_types_include_details_and_fill_object(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        assert "Details" in td["types"]
        assert "FillObject" in td["types"]

        details_field_names = [f["name"] for f in td["types"]["Details"]]
        for name in ("action", "market", "betting", "stake", "worstOdds",
                     "worstReturning", "fills"):
            assert name in details_field_names, f"missing Details field {name}"

        fill_field_names = [f["name"] for f in td["types"]["FillObject"]]
        for name in ("stakeWei", "marketHash", "baseToken", "desiredOdds",
                     "oddsSlippage", "isTakerBettingOutcomeOne", "fillSalt",
                     "beneficiary", "beneficiaryType", "cashOutTarget"):
            assert name in fill_field_names, f"missing FillObject field {name}"

    def test_message_baseToken_is_usdc(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        assert td["message"]["fills"]["baseToken"] == "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"

    def test_message_market_hash_round_trips(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=True,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        # Memory reference: marketHash is bytes32 — accept either bytes or 0x-hex
        mh = td["message"]["fills"]["marketHash"]
        if isinstance(mh, bytes):
            assert mh.hex() == FIXED_MARKET_HASH.replace("0x", "")
        else:
            assert mh.lower() == FIXED_MARKET_HASH.lower()

    def test_stake_display_string_formatting(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=25.0,
            taker_decimal_odds=1.85,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        # Per memory ref: stake/worstOdds/worstReturning are display strings
        assert td["message"]["stake"] == "$25.00"

    def test_stakeWei_uses_six_decimal_usdc(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=25.0,
            taker_decimal_odds=1.85,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        # USDC has 6 decimals — $25 = 25,000,000 atomic
        assert td["message"]["fills"]["stakeWei"] == str(25 * 10 ** 6)

    def test_desiredOdds_snapped_to_ladder(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=25.0,
            taker_decimal_odds=1.85,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        desired = int(td["message"]["fills"]["desiredOdds"])
        # Must be a multiple of the ladder step
        assert desired % signing.LADDER_STEP == 0


# ── Signature roundtrip ───────────────────────────────────────────────────────

class TestSignatureRoundtrip:

    def test_signature_recovers_signer_address(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        sig = signing.sign_fill_order(td, TEST_PRIVATE_KEY)

        assert sig.startswith("0x")
        assert len(sig) == 132  # 0x + 130 hex chars (65 bytes)

        # Recover the signer using eth_account directly
        encoded = encode_typed_data(
            domain_data=td["domain"],
            message_types={k: v for k, v in td["types"].items() if k != "EIP712Domain"},
            message_data=td["message"],
        )
        recovered = Account.recover_message(encoded, signature=sig)
        assert recovered.lower() == TEST_ADDRESS.lower()

    def test_signature_deterministic_for_fixed_inputs(self):
        td = signing.build_typed_data(
            market_hash=FIXED_MARKET_HASH,
            stake_usd=FIXED_STAKE_USD,
            taker_decimal_odds=FIXED_TAKER_DECIMAL_ODDS,
            is_taker_betting_outcome_one=False,
            taker_address=TEST_ADDRESS,
            fill_salt=FIXED_FILL_SALT,
        )
        sig_a = signing.sign_fill_order(td, TEST_PRIVATE_KEY)
        sig_b = signing.sign_fill_order(td, TEST_PRIVATE_KEY)
        assert sig_a == sig_b
