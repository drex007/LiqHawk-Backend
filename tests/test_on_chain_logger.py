"""OnChainLogger smoke tests — no real RPC, no real contract.

These verify wiring + disabled-mode behavior. The full deploy+log integration
is covered by `contracts/script/deploy.py` running against Mantle Sepolia.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.core.schema import Position
from app.services.cascade import detect_zones
from app.services.on_chain_logger import OnChainLogger


def _settings(**overrides) -> Settings:
    base = dict(
        mantle_network="mainnet",
        sepolia_deployer_private_key="",
        cascade_logger_address="",
    )
    base.update(overrides)
    return Settings(**base)


def test_disabled_when_no_key_or_address():
    logger = OnChainLogger(_settings())
    assert logger.enabled is False


def test_disabled_when_only_key_set():
    logger = OnChainLogger(_settings(sepolia_deployer_private_key="0x" + "11" * 32))
    assert logger.enabled is False


def test_disabled_when_only_address_set():
    logger = OnChainLogger(_settings(cascade_logger_address="0x" + "ab" * 20))
    assert logger.enabled is False


async def test_log_zones_noop_when_disabled():
    logger = OnChainLogger(_settings())
    positions = [
        Position(
            position_id=str(i),
            owner=f"0x{i:040x}",
            health_factor=Decimal("1.02"),
            total_collateral_usd=Decimal("10000"),
            total_debt_usd=Decimal("9000"),
            dominant_collateral="mETH",
        )
        for i in range(3)
    ]
    zones = detect_zones(positions)
    assert len(zones) == 1
    hashes = await logger.log_zones(zones, block_number=100)
    assert hashes == []
