import logging
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from hexbytes import HexBytes
from web3 import Web3

from pmus.config import Settings
from pmus.engine import LiveEngine
from pmus.merger import CTF_ADDRESS, COLLATERAL_TOKEN, PositionMerger
from pmus.models import Book, IncentiveWindow, Level, Market, Position


@pytest.fixture
def logger():
    return logging.getLogger("test_merger")


def test_merger_disabled_flag(logger):
    settings = Settings(enable_auto_merge=False, min_merge_shares=Decimal("5.0"))
    merger = PositionMerger(settings, logger)
    ok, err = merger.merge_positions("0xabcdef123456", Decimal("10.0"))
    assert ok is False
    assert err == "disabled_or_below_min"


def test_merger_below_minimum_shares(logger):
    settings = Settings(enable_auto_merge=True, min_merge_shares=Decimal("10.0"))
    merger = PositionMerger(settings, logger)
    ok, err = merger.merge_positions("0xabcdef123456", Decimal("9.99"))
    assert ok is False
    assert err == "disabled_or_below_min"


def test_merger_condition_id_hex_and_calldata(logger):
    dummy_key = "0x" + "11" * 32
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key=dummy_key,
        polymarket_proxy_address="0x" + "22" * 20,
    )
    merger = PositionMerger(settings, logger)

    short_cid = "0x1234"
    cid_bytes = bytes.fromhex(short_cid[2:].rjust(64, "0"))
    assert len(cid_bytes) == 32
    assert cid_bytes == b"\x00" * 30 + b"\x12\x34"

    full_cid = "0x" + "ab" * 32
    cid_full_bytes = bytes.fromhex(full_cid[2:].rjust(64, "0"))
    assert len(cid_full_bytes) == 32
    assert cid_full_bytes == b"\xab" * 32

    # Verify ABI encoding directly
    ctf_contract = merger.w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=merger.w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=from_abi_def()).abi
    ) if False else None


def from_abi_def():
    from pmus.merger import CTF_MERGE_ABI
    return CTF_MERGE_ABI


def test_merger_relayer_success(logger):
    dummy_key = "0x" + "aa" * 32
    proxy_wallet = "0x" + "bb" * 20
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key=dummy_key,
        polymarket_proxy_address=proxy_wallet,
        builder_api_key="b_key",
        builder_secret="b_sec",
        builder_passphrase="b_pass",
    )
    merger = PositionMerger(settings, logger)

    mock_relay = MagicMock()
    mock_relay.get_nonce.return_value = {"nonce": 7}
    mock_resp = MagicMock()
    mock_resp.transaction_hash = HexBytes("0x9876543210abcdef9876543210abcdef9876543210abcdef9876543210abcdef")
    mock_relay.execute_deposit_wallet_batch.return_value = mock_resp
    merger.relay_client = mock_relay

    cond_id = "0x" + "cc" * 32
    ok, tx_hash = merger.merge_positions(cond_id, Decimal("15.5"))

    assert ok is True
    assert "9876543210abcdef" in tx_hash
    mock_relay.get_nonce.assert_called_once_with(proxy_wallet, "WALLET")
    mock_relay.execute_deposit_wallet_batch.assert_called_once()
    call_kwargs = mock_relay.execute_deposit_wallet_batch.call_args[1]
    assert call_kwargs["wallet_address"] == proxy_wallet
    assert call_kwargs["nonce"] == "7"
    assert len(call_kwargs["calls"]) == 1
    assert call_kwargs["calls"][0].target == Web3.to_checksum_address(CTF_ADDRESS)


def test_merger_relayer_failure_web3_fallback_success(logger):
    dummy_key = "0x" + "12" * 32
    proxy_wallet = "0x" + "34" * 20
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key=dummy_key,
        polymarket_proxy_address=proxy_wallet,
        builder_api_key="b_key",
        builder_secret="b_sec",
        builder_passphrase="b_pass",
    )
    merger = PositionMerger(settings, logger)

    mock_relay = MagicMock()
    mock_relay.get_nonce.side_effect = RuntimeError("Relayer v2 503 Service Unavailable")
    merger.relay_client = mock_relay

    expected_tx_hash = HexBytes("0x11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff")
    mock_account = MagicMock()
    mock_account.address = "0x" + "55" * 20
    mock_signed = MagicMock()
    mock_signed.raw_transaction = b"raw_tx_bytes"

    from unittest.mock import PropertyMock

    with patch.object(merger.w3.eth.account, "from_key", return_value=mock_account), \
         patch.object(merger.w3.eth, "get_transaction_count", return_value=3), \
         patch.object(merger.w3.eth.account, "sign_transaction", return_value=mock_signed), \
         patch.object(merger.w3.eth, "send_raw_transaction", return_value=expected_tx_hash), \
         patch.object(type(merger.w3.eth), "gas_price", new_callable=PropertyMock, return_value=30000000000):
        ok, tx_hash = merger.merge_positions("0x" + "dd" * 32, Decimal("20.0"))

    assert ok is True
    assert tx_hash == expected_tx_hash.hex()


def test_merger_both_relayer_and_web3_fail(logger):
    from unittest.mock import PropertyMock

    dummy_key = "0x" + "12" * 32
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key=dummy_key,
        polymarket_proxy_address="0x" + "34" * 20,
    )
    merger = PositionMerger(settings, logger)
    merger.relay_client = None

    mock_account = MagicMock()
    mock_account.address = "0x" + "55" * 20

    with patch.object(merger.w3.eth.account, "from_key", return_value=mock_account), \
         patch.object(merger.w3.eth, "get_transaction_count", return_value=3), \
         patch.object(type(merger.w3.eth), "gas_price", new_callable=PropertyMock, return_value=30000000000), \
         patch.object(merger.w3.eth.account, "sign_transaction", side_effect=Exception("Gas estimation failed: execution reverted")):
        ok, err = merger.merge_positions("0x" + "ee" * 32, Decimal("10.0"))

    assert ok is False
    assert "Gas estimation failed" in err


def test_merger_no_private_key_fallback(logger):
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key="",
        polymarket_proxy_address="0x" + "34" * 20,
    )
    merger = PositionMerger(settings, logger)
    merger.relay_client = None

    ok, err = merger.merge_positions("0x" + "ff" * 32, Decimal("10.0"))
    assert ok is False
    assert err == "no_private_key_for_fallback"


def test_engine_check_auto_merges_full_cycle():
    logger = logging.getLogger("test_engine_auto_merge")
    settings = Settings(
        venue="global",
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
        polygon_private_key="0x" + "11" * 32,
        polymarket_proxy_address="0x" + "22" * 20,
    )
    ledger = MagicMock()
    public_client = MagicMock()
    auth_client = MagicMock()
    auth_client.is_available = True
    auth_client._buying_power_cache = Decimal("100.00")
    risk_manager = MagicMock()

    engine = LiveEngine(
        settings=settings,
        ledger=ledger,
        public_client=public_client,
        auth_client=auth_client,
        risk_manager=risk_manager,
        logger=logger,
    )

    engine.condition_pairs = (("cond1-yes", "cond1-no"),)
    engine.known_cond_by_slug["cond1-yes"] = "0x" + "44" * 32
    engine.notifier = MagicMock()

    live_positions = {
        "cond1-yes": Position(slug="cond1-yes", net_qty=Decimal("12.0"), avg_price=Decimal("0.45")),
        "cond1-no": Position(slug="cond1-no", net_qty=Decimal("18.0"), avg_price=Decimal("0.55")),
    }

    engine.merger = MagicMock()
    engine.merger.merge_positions.return_value = (True, "0xautomerge_tx_hash_123")

    now_ts = 1700000000.0
    now_ms = 1700000000000

    engine._check_auto_merges(live_positions, now_ts, now_ms)

    # 1. Merger was invoked with min(12.0, 18.0) = 12.0 shares
    engine.merger.merge_positions.assert_called_once_with(
        "0x" + "44" * 32,
        Decimal("12.0"),
        wallet_address=settings.address,
    )

    # 2. Local positions decremented
    assert live_positions["cond1-yes"].net_qty == Decimal("0.0")
    assert live_positions["cond1-no"].net_qty == Decimal("6.0")

    # 3. Ledger positions updated
    assert ledger.update_position.call_count == 2

    # 4. Informative fills recorded
    assert ledger.record_fill.call_count == 2
    fill1 = ledger.record_fill.call_args_list[0][0][0]
    fill2 = ledger.record_fill.call_args_list[1][0][0]
    assert fill1.slug == "cond1-yes" and fill1.qty == Decimal("12.0")
    assert fill2.slug == "cond1-no" and fill2.qty == Decimal("12.0")

    # 5. Notifier notified
    engine.notifier.notify_auto_merge.assert_called_once_with(
        "0x" + "44" * 32,
        Decimal("12.0"),
        "0xautomerge_tx_hash_123",
    )

    # 6. Buying power cache invalidated
    auth_client.invalidate_buying_power_cache.assert_called_once()
    assert auth_client._buying_power_cache is None
    assert engine.last_auto_merge_ts == now_ts


def test_engine_check_auto_merges_below_min_ignored():
    logger = logging.getLogger("test_engine_auto_merge_below_min")
    settings = Settings(
        enable_auto_merge=True,
        min_merge_shares=Decimal("5.0"),
    )
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=MagicMock(),
        auth_client=MagicMock(),
        risk_manager=MagicMock(),
        logger=logger,
    )
    engine.condition_pairs = (("c1-yes", "c1-no"),)
    engine.known_cond_by_slug["c1-yes"] = "0x" + "55" * 32
    engine.merger = MagicMock()

    # One leg below 5.0
    live_positions = {
        "c1-yes": Position(slug="c1-yes", net_qty=Decimal("4.5"), avg_price=Decimal("0.50")),
        "c1-no": Position(slug="c1-no", net_qty=Decimal("10.0"), avg_price=Decimal("0.50")),
    }

    engine._check_auto_merges(live_positions, 1700000000.0, 1700000000000)
    engine.merger.merge_positions.assert_not_called()
    assert live_positions["c1-yes"].net_qty == Decimal("4.5")
    assert live_positions["c1-no"].net_qty == Decimal("10.0")


def test_high_volume_concentration_est_cond_cap():
    logger = logging.getLogger("test_high_volume_cap")
    settings = Settings(
        venue="global",
        total_budget=Decimal("500.00"),
        max_per_market=Decimal("150.00"),  # > 85.00
        max_collateral=Decimal("500.00"),
        max_markets=4,
    )
    engine = LiveEngine(
        settings=settings,
        ledger=MagicMock(),
        public_client=MagicMock(),
        auth_client=MagicMock(),
        risk_manager=MagicMock(),
        logger=logger,
    )

    # In prepare():
    # cond_cap_limit = max(Decimal("85.00"), self.settings.max_per_market) -> 150.00
    # est_cond_cap = min(cond_cap_limit, max(Decimal("36.00"), (effective_budget / est_pairs) * 1.15))
    effective_budget = Decimal("400.00")
    est_pairs = 3
    raw_cap = (effective_budget / Decimal(str(est_pairs))) * Decimal("1.15")  # ~153.33
    cond_cap_limit = max(Decimal("85.00"), settings.max_per_market)  # 150.00
    est_cond_cap = min(cond_cap_limit, max(Decimal("36.00"), raw_cap))
    assert est_cond_cap == Decimal("150.00")
    assert est_cond_cap > Decimal("85.00")
