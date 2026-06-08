"""Repository layer — all Mongo queries live here.

Why a separate file:
  - API handlers should be thin: parse request → call repo → return response.
    Burying queries inside handlers makes them untestable and entangled.
  - Swapping storage (e.g., Mongo → Postgres) becomes a one-file change.
  - Repository functions are easy to unit-test with mongomock-motor.

Convention:
  - Each function takes `db` as first arg (passed in from the API via Depends)
  - Returns Pydantic models, never raw dicts — callers don't touch Mongo internals
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.logging_setup import get_logger
from app.core.schema import PoolSnapshot, Position

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Snapshot writes (called by the background polling task)
# ---------------------------------------------------------------------------


async def save_snapshot(db: AsyncIOMotorDatabase, snapshot: PoolSnapshot) -> None:
    """Persist a full snapshot.

    Storage strategy:
      - snapshots collection: one document per polling cycle (block-level metadata)
      - positions collection: one document per position per cycle (for history)

    The duplication is intentional — querying "show me position 42 over time"
    is a single indexed scan, no joins. Mongo is denormalized by design.
    """
    snap_doc = _snapshot_to_doc(snapshot)
    await db.snapshots.insert_one(snap_doc)

    if snapshot.positions:
        pos_docs = [
            _position_to_doc(p, snapshot.captured_at, snapshot.block_number)
            for p in snapshot.positions
        ]
        # ordered=False: if one insert fails, the others still go through.
        # We don't want a single bad position to lose the entire snapshot.
        await db.positions.insert_many(pos_docs, ordered=False)

    log.info(
        "snapshot_persisted",
        block=snapshot.block_number,
        positions=len(snapshot.positions),
    )


# ---------------------------------------------------------------------------
# Snapshot reads (called by the API)
# ---------------------------------------------------------------------------


async def get_latest_snapshot(db: AsyncIOMotorDatabase) -> PoolSnapshot | None:
    """Most recent snapshot, fully hydrated with its positions."""
    snap_doc = await db.snapshots.find_one(sort=[("block_number", -1)])
    if not snap_doc:
        return None

    pos_cursor = db.positions.find({"captured_at": snap_doc["captured_at"]})
    pos_docs = await pos_cursor.to_list(length=None)
    snap_doc["positions"] = [_doc_to_position(d) for d in pos_docs]

    return _doc_to_snapshot(snap_doc)


async def list_snapshots(
    db: AsyncIOMotorDatabase,
    limit: int = 50,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """Lightweight snapshot listing — for the /snapshots endpoint.

    Returns dicts not Pydantic objects because the API maps them to
    SnapshotSummary directly. No need to construct full PoolSnapshot here.
    """
    cursor = (
        db.snapshots.find({})
        .sort("block_number", -1)
        .skip(skip)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


# ---------------------------------------------------------------------------
# Position reads
# ---------------------------------------------------------------------------


async def get_position_history(
    db: AsyncIOMotorDatabase,
    position_id: str,
    limit: int = 100,
) -> list[Position]:
    """All historical states of a single position, newest first."""
    cursor = (
        db.positions.find({"position_id": position_id})
        .sort("captured_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return [_doc_to_position(d) for d in docs]


async def get_current_at_risk(
    db: AsyncIOMotorDatabase,
    min_risk: str = "HIGH",
) -> list[Position]:
    """All positions in the LATEST snapshot at or above the given risk level."""
    latest_snap = await db.snapshots.find_one(sort=[("block_number", -1)])
    if not latest_snap:
        return []

    risk_order = ["SAFE", "MEDIUM", "HIGH", "CRITICAL"]
    allowed = risk_order[risk_order.index(min_risk):]

    cursor = db.positions.find(
        {
            "captured_at": latest_snap["captured_at"],
            "risk_level": {"$in": allowed},
        }
    ).sort("health_factor", 1)  # most at-risk first

    docs = await cursor.to_list(length=None)
    return [_doc_to_position(d) for d in docs]


# ---------------------------------------------------------------------------
# Document <-> Pydantic converters
# ---------------------------------------------------------------------------
# Mongo stores BSON, which doesn't support Python's Decimal natively. We
# convert to string on write, back to Decimal on read. Lossless round-trip.


def _decimal_to_str(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def _snapshot_to_doc(snap: PoolSnapshot) -> dict[str, Any]:
    return {
        "block_number": snap.block_number,
        "block_timestamp": snap.block_timestamp,
        "chain_id": snap.chain_id,
        "network": snap.network,
        "captured_at": snap.captured_at,
        "total_positions": len(snap.positions),
        "at_risk_count": len(snap.at_risk()),
        "critical_count": sum(1 for p in snap.positions if p.risk_level == "CRITICAL"),
    }


def _leg_to_doc(leg: Any) -> dict[str, Any]:
    """Serialize a CollateralLeg/DebtLeg to a Mongo-safe dict."""
    return {
        "pool": leg.pool,
        "token": leg.token,
        "symbol": leg.symbol,
        "amount_shares": _decimal_to_str(leg.amount_shares),
        "amount_underlying": _decimal_to_str(leg.amount_underlying)
        if leg.amount_underlying is not None
        else None,
        "amount_usd": _decimal_to_str(leg.amount_usd)
        if leg.amount_usd is not None
        else None,
    }


def _position_to_doc(
    pos: Position, captured_at: datetime, block_number: int
) -> dict[str, Any]:
    return {
        "position_id": pos.position_id,
        "protocol": pos.protocol,
        "owner": pos.owner,
        "mode": pos.mode,
        "block_number": block_number,
        "captured_at": captured_at,
        "health_factor": _decimal_to_str(pos.health_factor),
        "total_collateral_usd": _decimal_to_str(pos.total_collateral_usd),
        "total_debt_usd": _decimal_to_str(pos.total_debt_usd),
        "risk_level": pos.risk_level,
        "dominant_collateral": pos.dominant_collateral,
        "collateral": [_leg_to_doc(leg) for leg in pos.collateral],
        "debt": [_leg_to_doc(leg) for leg in pos.debt],
    }


def _doc_to_leg(doc: dict[str, Any], leg_cls: type) -> Any:
    return leg_cls(
        pool=doc.get("pool", ""),
        token=doc.get("token", ""),
        symbol=doc.get("symbol"),
        amount_shares=Decimal(doc.get("amount_shares", "0")),
        amount_underlying=Decimal(doc["amount_underlying"])
        if doc.get("amount_underlying") is not None
        else None,
        amount_usd=Decimal(doc["amount_usd"])
        if doc.get("amount_usd") is not None
        else None,
    )


def _doc_to_position(doc: dict[str, Any]) -> Position:
    from app.core.schema import CollateralLeg, DebtLeg
    return Position(
        position_id=str(doc["position_id"]),
        protocol=doc.get("protocol", "init"),  # default to init for back-compat
        owner=doc["owner"],
        mode=doc.get("mode"),
        collateral=[_doc_to_leg(d, CollateralLeg) for d in doc.get("collateral", [])],
        debt=[_doc_to_leg(d, DebtLeg) for d in doc.get("debt", [])],
        health_factor=Decimal(doc["health_factor"]),
        total_collateral_usd=Decimal(doc["total_collateral_usd"]),
        total_debt_usd=Decimal(doc["total_debt_usd"]),
        dominant_collateral=doc.get("dominant_collateral"),
    )


def _doc_to_snapshot(doc: dict[str, Any]) -> PoolSnapshot:
    return PoolSnapshot(
        block_number=doc["block_number"],
        block_timestamp=doc["block_timestamp"].replace(tzinfo=timezone.utc)
        if doc["block_timestamp"].tzinfo is None
        else doc["block_timestamp"],
        chain_id=doc["chain_id"],
        network=doc["network"],
        positions=doc.get("positions", []),
        captured_at=doc["captured_at"].replace(tzinfo=timezone.utc)
        if doc["captured_at"].tzinfo is None
        else doc["captured_at"],
    )
