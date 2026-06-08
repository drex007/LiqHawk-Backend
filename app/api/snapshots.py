"""Snapshot endpoints.

GET /snapshots         → paginated list of recent snapshots (summary only)
GET /snapshots/latest  → most recent snapshot, fully hydrated with positions
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.schema import PoolSnapshot, SnapshotSummary
from app.db import repositories as repo
from app.db.mongo import get_db

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("", response_model=list[SnapshotSummary])
async def list_snapshots(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),  # capped — no unbounded queries
    skip: int = Query(0, ge=0),
) -> list[SnapshotSummary]:
    """Recent snapshots, newest first. Lightweight — no position lists."""
    docs = await repo.list_snapshots(db, limit=limit, skip=skip)
    return [
        SnapshotSummary(
            block_number=d["block_number"],
            block_timestamp=d["block_timestamp"],
            captured_at=d["captured_at"],
            total_positions=d.get("total_positions", 0),
            at_risk_count=d.get("at_risk_count", 0),
            critical_count=d.get("critical_count", 0),
        )
        for d in docs
    ]


@router.get("/latest", response_model=PoolSnapshot)
async def latest_snapshot(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PoolSnapshot:
    """The most recent snapshot, fully hydrated."""
    snap = await repo.get_latest_snapshot(db)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail="No snapshots yet. Wait for the first poll cycle to complete.",
        )
    return snap
