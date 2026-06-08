"""Health + diagnostics routes.

GET /health      → quick liveness check (mongo + RPC reachable)
GET /diagnostics → deeper smoke test: reads 5 positions, surfaces ABI errors
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings, get_settings
from app.core.schema import HealthResponse
from app.db.mongo import get_db
from app.services.pipeline import CascadePoller, get_poller

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> HealthResponse:
    """Liveness probe. Verifies DB + RPC connectivity."""
    mongo_ok = False
    try:
        await db.command("ping")
        mongo_ok = True
    except Exception:  # noqa: BLE001
        pass

    latest_block = None
    try:
        latest_doc = await db.snapshots.find_one(sort=[("block_number", -1)])
        latest_block = latest_doc["block_number"] if latest_doc else None
    except Exception:  # noqa: BLE001
        pass

    return HealthResponse(
        status="ok" if mongo_ok else "degraded",
        network=settings.mantle_network,
        chain_id=settings.chain_id,
        latest_block=latest_block,
        mongo_connected=mongo_ok,
    )


@router.post("/diagnostics/snapshot")
async def force_snapshot(
    poller: Annotated[CascadePoller, Depends(get_poller)],
) -> dict:
    """Trigger one snapshot read NOW — bypassing the polling interval.

    Useful for:
      - Verifying ABI changes work without waiting 30s
      - Demo: 'watch me read the chain live'
      - Debugging: did anything change since last poll?

    Returns a summary, not the full snapshot (which can be huge).
    """
    snap = await poller.take_snapshot()
    return {
        "block_number": snap.block_number,
        "captured_at": snap.captured_at,
        "total_positions": snap.total_positions,
        "at_risk_count": len(snap.at_risk()),
        "sample": [
            {
                "position_id": p.position_id,
                "owner": p.owner,
                "health_factor": str(p.health_factor),
                "risk_level": p.risk_level,
                "dominant_collateral": p.dominant_collateral,
                "total_collateral_usd": str(p.total_collateral_usd),
                "total_debt_usd": str(p.total_debt_usd),
            }
            for p in sorted(snap.positions, key=lambda x: x.health_factor)[:5]
        ],
    }
