"""Cascade zone endpoint.

GET /cascade/zones → predicted cascade clusters from the latest snapshot
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db import repositories as repo
from app.db.mongo import get_db
from app.services.cascade import detect_zones, estimate_time_to_cascade_minutes

router = APIRouter(prefix="/cascade", tags=["cascade"])


@router.get("/zones")
async def cascade_zones(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    at_risk_hf: float = Query(1.15, gt=1.0, le=2.0,
                              description="HF threshold below which positions count as at-risk"),
    min_cluster_size: int = Query(3, ge=2, le=20,
                                  description="Minimum positions sharing a collateral to count as a zone"),
) -> dict:
    """Detect cascade zones in the latest snapshot.

    Returns zones sorted by probability descending, each with severity,
    estimated time-to-cascade, and the constituent at-risk positions.
    """
    snap = await repo.get_latest_snapshot(db)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail="No snapshots yet. Wait for the first poll cycle to complete.",
        )

    zones = detect_zones(
        snap.positions,
        at_risk_hf=Decimal(str(at_risk_hf)),
        min_cluster_size=min_cluster_size,
    )

    return {
        "block_number": snap.block_number,
        "captured_at": snap.captured_at,
        "total_positions_scanned": len(snap.positions),
        "num_zones": len(zones),
        "zones": [
            {**z.to_dict(), "time_to_cascade_minutes": estimate_time_to_cascade_minutes(z)}
            for z in zones
        ],
    }
