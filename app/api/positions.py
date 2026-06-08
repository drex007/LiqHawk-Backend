"""Position endpoints.

GET /positions/at-risk        → current positions at HIGH or CRITICAL risk
GET /positions/by-risk        → paginated positions from latest snapshot, sorted by HF asc
GET /positions/{id}/history   → time series for one position
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.schema import Position
from app.db import repositories as repo
from app.db.mongo import get_db

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/by-risk")
async def by_risk(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    protocol: Literal["all", "init", "lendle"] = "all",
    risk: Literal["any", "CRITICAL", "HIGH", "MEDIUM", "SAFE"] = "any",
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
) -> dict:
    """Paginated positions from the latest snapshot, sorted by health-factor ascending.

    `totals` always reports the full per-risk counts for the (protocol, risk)
    filter — independent of pagination — so the client can render stat cards
    accurately even when paging through a slice.
    """
    snap = await repo.get_latest_snapshot(db)
    if snap is None:
        raise HTTPException(404, detail="No snapshot yet — wait for first poll cycle.")

    positions = (
        snap.positions
        if protocol == "all"
        else [p for p in snap.positions if p.protocol == protocol]
    )

    totals = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "SAFE": 0}
    for p in positions:
        totals[p.risk_level] += 1

    filtered = positions if risk == "any" else [p for p in positions if p.risk_level == risk]
    filtered.sort(key=lambda x: x.health_factor)

    total = len(filtered)
    page = filtered[skip : skip + limit]
    items = [
        {
            "position_id": p.position_id,
            "protocol": p.protocol,
            "owner": p.owner,
            "health_factor": str(p.health_factor),
            "risk_level": p.risk_level,
            "dominant_collateral": p.dominant_collateral,
            "total_collateral_usd": str(p.total_collateral_usd),
            "total_debt_usd": str(p.total_debt_usd),
            "distance_to_liquidation_pct": str(p.distance_to_liquidation_pct),
        }
        for p in page
    ]

    return {
        "block_number": snap.block_number,
        "captured_at": snap.captured_at,
        "protocol_filter": protocol,
        "risk_filter": risk,
        "totals": totals,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_more": skip + len(items) < total,
        "positions": items,
    }


@router.get("/at-risk", response_model=list[Position])
async def at_risk(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    min_risk: Literal["MEDIUM", "HIGH", "CRITICAL"] = "HIGH",
) -> list[Position]:
    """All positions at or above the given risk level in the latest snapshot.

    Sorted by health factor ascending (most critical first).
    """
    return await repo.get_current_at_risk(db, min_risk=min_risk)


@router.get("/{position_id}/history", response_model=list[Position])
async def position_history(
    position_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    limit: int = Query(100, ge=1, le=500),
) -> list[Position]:
    """Time series of a single position's state across snapshots.

    INIT position IDs are uint256 (77-78 digit numbers); pass them as strings.
    """
    history = await repo.get_position_history(db, position_id=position_id, limit=limit)
    if not history:
        raise HTTPException(status_code=404, detail=f"No history for position {position_id}")
    return history
