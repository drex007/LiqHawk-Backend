"""LendleReader tests — no real RPC, no real Mantle.

Strategy: stub MantleClient with a tiny in-memory state machine that lets us
exercise the discovery/read paths without touching the network. The web3
contract functions are mocked at the .functions.<name>(...).call() boundary.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.cascade import detect_zones
from app.services.lendle import LendleReader, _MAX_UINT256


@pytest.fixture
def cache_path(tmp_path):
    """Per-test cache file so the on-disk borrower cache doesn't leak across tests."""
    return tmp_path / "lendle_borrowers.json"


def _make_client(*, latest_block: int = 1_000_000, borrow_logs: list | None = None):
    """Build a fake MantleClient that supports just the methods LendleReader uses."""
    client = MagicMock(name="MantleClient")
    client.w3.eth.block_number = latest_block
    client.w3.eth.get_logs.return_value = borrow_logs or []
    return client


def _make_lending_pool_mock(reserves: list[str], account_data: dict[str, tuple]):
    """Return a fake LendingPool contract whose functions return canned data."""
    pool = MagicMock(name="LendingPool")
    pool.functions.getReservesList.return_value.call.return_value = reserves

    def _get_user_account_data(addr):
        m = MagicMock()
        m.call.return_value = account_data.get(
            addr,
            (0, 0, 0, 0, 0, _MAX_UINT256),  # no debt → max-HF sentinel
        )
        return m

    pool.functions.getUserAccountData.side_effect = _get_user_account_data
    return pool


@pytest.fixture
def settings():
    from app.core.config import Settings
    return Settings(enable_lendle=True, lendle_discovery_block_window=10_000)


def test_borrower_discovery_dedups_users(settings, cache_path):
    user_a = "0x" + "aa" * 20
    user_b = "0x" + "bb" * 20
    # For Repay events, user is at topic[2]; the address is the last 20 bytes
    # of the 32-byte topic (left-padded zeros).
    def topic_for(addr):
        return bytes.fromhex(addr[2:].rjust(64, "0"))
    logs = [
        {"topics": [b"\x00", b"\x00", topic_for(user_a)]},
        {"topics": [b"\x00", b"\x00", topic_for(user_a)]},  # duplicate
        {"topics": [b"\x00", b"\x00", topic_for(user_b)]},
    ]
    client = _make_client(borrow_logs=logs)
    reader = LendleReader(client, settings, borrower_cache_path=cache_path)
    reader.lending_pool = _make_lending_pool_mock([], {})

    borrowers = reader.discover_borrowers()
    # Compare in checksum form — discover_borrowers stores checksummed.
    from web3 import Web3
    assert Web3.to_checksum_address(user_a) in borrowers
    assert Web3.to_checksum_address(user_b) in borrowers
    assert len(borrowers) == 2


def test_get_user_account_normalizes_no_debt():
    from web3 import Web3
    settings = MagicMock(lendle_discovery_block_window=1000)
    client = _make_client()
    reader = LendleReader(client, settings, borrower_cache_path=None)
    # The mock dict is keyed by the *checksummed* address — that's what
    # get_user_account passes into getUserAccountData.
    addr = Web3.to_checksum_address("0x" + "ab" * 20)
    reader.lending_pool = _make_lending_pool_mock(
        [], {addr: (0, 0, 0, 0, 0, _MAX_UINT256)},
    )
    coll, debt, hf = reader.get_user_account(addr)
    assert coll == 0
    assert debt == 0
    assert hf == Decimal("999")  # sentinel for "infinite"


def test_get_user_account_normal_path():
    from web3 import Web3
    settings = MagicMock(lendle_discovery_block_window=1000)
    client = _make_client()
    reader = LendleReader(client, settings, borrower_cache_path=None)
    user = Web3.to_checksum_address("0x" + "ab" * 20)
    # 1000 USD collateral, 500 USD debt, HF = 1.95 → all scaled by 1e18
    reader.lending_pool = _make_lending_pool_mock(
        [],
        {user: (int(1000 * 10**18), int(500 * 10**18), 0, 0, 0, int(1.95 * 10**18))},
    )
    coll, debt, hf = reader.get_user_account(user)
    assert coll == Decimal(1000)
    assert debt == Decimal(500)
    assert abs(hf - Decimal("1.95")) < Decimal("0.0001")


def test_read_all_positions_skips_no_debt_users(settings, cache_path):
    from web3 import Web3
    user_active = Web3.to_checksum_address("0x" + "11" * 20)
    user_empty = Web3.to_checksum_address("0x" + "22" * 20)
    client = _make_client()
    reader = LendleReader(client, settings, borrower_cache_path=cache_path)
    reader._known_borrowers = {user_active, user_empty}
    reader._last_discovery_to_block = 999_999  # short-circuit discovery
    reader.lending_pool = _make_lending_pool_mock(
        [],
        {
            user_active: (int(800 * 10**18), int(600 * 10**18), 0, 0, 0, int(1.33 * 10**18)),
            user_empty:  (0, 0, 0, 0, 0, _MAX_UINT256),
        },
    )

    positions = reader.read_all_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.protocol == "lendle"
    assert p.position_id == user_active
    assert p.total_debt_usd == Decimal(600)


def test_protocol_field_propagates_to_cluster_buckets(settings):
    """Lendle and INIT positions go into separate buckets in cascade clustering."""
    from app.core.schema import Position
    positions = [
        Position(position_id=f"init-{i}", protocol="init", owner=f"0x{i:040x}",
                 health_factor=Decimal("1.02"), total_collateral_usd=Decimal(10000),
                 total_debt_usd=Decimal(9000), dominant_collateral="mETH")
        for i in range(3)
    ] + [
        Position(position_id=f"0x{i:040x}", protocol="lendle", owner=f"0x{i:040x}",
                 health_factor=Decimal("1.04"), total_collateral_usd=Decimal(20000),
                 total_debt_usd=Decimal(18000), dominant_collateral="USDC")
        for i in range(3)
    ]
    zones = detect_zones(positions)
    assert len(zones) == 2
    syms = {z.collateral_symbol for z in zones}
    assert syms == {"mETH", "USDC"}
