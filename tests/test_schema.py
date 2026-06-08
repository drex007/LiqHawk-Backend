"""Pure tests for schema risk math — no network, no DB."""

from decimal import Decimal

from app.core.schema import Position


def _pos(hf: str, coll: str = "10000", debt: str = "5000") -> Position:
    return Position(
        position_id="1",
        owner="0x0000000000000000000000000000000000000000",
        health_factor=Decimal(hf),
        total_collateral_usd=Decimal(coll),
        total_debt_usd=Decimal(debt),
    )


def test_risk_level_safe():
    assert _pos("1.50").risk_level == "SAFE"


def test_risk_level_medium():
    assert _pos("1.10").risk_level == "MEDIUM"


def test_risk_level_high():
    assert _pos("1.02").risk_level == "HIGH"


def test_risk_level_critical():
    assert _pos("0.95").risk_level == "CRITICAL"


def test_distance_to_liquidation():
    p = _pos("1.05")
    assert p.distance_to_liquidation_pct == Decimal("5.00")


def test_leverage():
    p = _pos("1.5", coll="10000", debt="3000")
    assert p.leverage == Decimal("0.3")


def test_zero_collateral_leverage():
    p = _pos("0", coll="0", debt="0")
    assert p.leverage == Decimal(0)
