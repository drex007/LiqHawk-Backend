"""Pydantic schemas.

These serve THREE roles at once in a FastAPI + Mongo app:
  1. Domain types — the shape of a Position in business logic
  2. Mongo documents — what gets stored (with .model_dump())
  3. API response models — what FastAPI serializes to JSON for clients

Keeping them all in one place means changing a field updates DB, API, and
internal logic together. No drift.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["SAFE", "MEDIUM", "HIGH", "CRITICAL"]

# Lending protocol identifier. Lower-case slugs so they're URL- and JSON-safe.
ProtocolId = Literal["init", "lendle"]


# Common config — used by every model in this file.
# arbitrary_types_allowed: lets us use Decimal cleanly
# json_encoders: how to serialize Decimal in JSON responses (default would error)
_BASE_CONFIG = ConfigDict(
    arbitrary_types_allowed=True,
    json_encoders={Decimal: str},
)


class CollateralLeg(BaseModel):
    """One collateral asset within a position.

    INIT positions hold collateral as LP-token shares of a lending pool. The
    pool wraps an underlying ERC-20 (e.g., the mETH pool wraps mETH). We track
    both: `pool` for protocol-level lookups, `token` for cascade clustering by
    underlying asset.
    """

    model_config = _BASE_CONFIG

    pool: str                       # Lending-pool contract address (LP wrapper)
    token: str                      # Underlying ERC-20 address (for clustering)
    symbol: str | None = None       # Underlying symbol (e.g., "mETH", "USDC")
    amount_shares: Decimal          # Raw LP-share amount as returned by getPosCollInfo
    amount_underlying: Decimal | None = None  # Underlying token units (post pool.toAmt)
    amount_usd: Decimal | None = None         # USD value at snapshot oracle price


class DebtLeg(BaseModel):
    """One debt asset within a position.

    INIT tracks debt as `shares` in the borrowing pool — multiply by the pool's
    debt-share price to get underlying amount. We mirror the collateral schema.
    """

    model_config = _BASE_CONFIG

    pool: str
    token: str
    symbol: str | None = None
    amount_shares: Decimal
    amount_underlying: Decimal | None = None
    amount_usd: Decimal | None = None


class Position(BaseModel):
    """A single INIT Capital position.

    INIT uses position-level health, not account-level — one wallet can own
    many positions, each with its own collateral, debt, and risk.

    position_id is stored as `str` because INIT's PosManager mints NFT IDs as
    uint256 (77-78 digit numbers), which overflow BSON int64.
    """

    model_config = _BASE_CONFIG

    position_id: str
    protocol: ProtocolId = "init"  # Which lending protocol issued this position
    owner: str
    mode: int | None = None  # INIT mode (1=general, 2=LST, etc.). Unused for Aave-style protocols.

    collateral: list[CollateralLeg] = Field(default_factory=list)
    debt: list[DebtLeg] = Field(default_factory=list)

    health_factor: Decimal       # < 1.0 = liquidatable
    total_collateral_usd: Decimal
    total_debt_usd: Decimal

    # Dominant collateral token symbol — used to cluster positions into cascade
    # zones (positions sharing the same dominant collateral liquidate together
    # when its price drops). Populated by InitCapitalReader.
    dominant_collateral: str | None = None

    # --- Computed properties (not stored, derived on access) ---

    @property
    def leverage(self) -> Decimal:
        if self.total_collateral_usd == 0:
            return Decimal(0)
        return self.total_debt_usd / self.total_collateral_usd

    @property
    def distance_to_liquidation_pct(self) -> Decimal:
        """Percentage points HF sits above 1.0. Negative = already liquidatable."""
        return (self.health_factor - Decimal(1)) * Decimal(100)

    @property
    def risk_level(self) -> RiskLevel:
        hf = self.health_factor
        if hf < Decimal("1.0"):
            return "CRITICAL"
        if hf < Decimal("1.05"):
            return "HIGH"
        if hf < Decimal("1.15"):
            return "MEDIUM"
        return "SAFE"


class PoolSnapshot(BaseModel):
    """One full read of INIT Capital state at a specific block.

    This is what gets stored in MongoDB per polling cycle and what the API
    serves to clients asking "what's the current risk landscape?"
    """

    model_config = _BASE_CONFIG

    block_number: int
    block_timestamp: datetime
    chain_id: int
    network: str
    positions: list[Position]
    captured_at: datetime  # When OUR pipeline took the snapshot (vs block time)

    @property
    def total_positions(self) -> int:
        return len(self.positions)

    def at_risk(self, hf_threshold: Decimal = Decimal("1.05")) -> list[Position]:
        return [p for p in self.positions if p.health_factor < hf_threshold]


# ==============================================================================
# API response models — what clients see when they hit the REST endpoints.
# Separate from internal types so we can evolve internals without breaking API.
# ==============================================================================


class HealthResponse(BaseModel):
    """GET /health — basic liveness."""

    status: Literal["ok", "degraded"]
    network: str
    chain_id: int
    latest_block: int | None = None
    mongo_connected: bool


class SnapshotSummary(BaseModel):
    """Lightweight snapshot view — for listing endpoints."""

    block_number: int
    block_timestamp: datetime
    captured_at: datetime
    total_positions: int
    at_risk_count: int
    critical_count: int


class PositionResponse(BaseModel):
    """Single position view — for /positions/{id} endpoint."""

    model_config = _BASE_CONFIG

    position_id: int
    owner: str
    health_factor: Decimal
    risk_level: RiskLevel
    total_collateral_usd: Decimal
    total_debt_usd: Decimal
    distance_to_liquidation_pct: Decimal
    captured_at: datetime
