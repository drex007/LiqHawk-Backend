"""API tests using FastAPI's TestClient and a mocked DB.

These don't hit a real Mongo or RPC — they verify routing, validation,
serialization, and the repo layer's contract.

Strategy: build a separate FastAPI app WITHOUT the production lifespan, so
no real Mongo/RPC connection is attempted. Mount the same routers. Override
get_db to return a mongomock-motor in-memory database.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.api import health, positions, snapshots
from app.db.mongo import get_db, mongo


def _build_test_app() -> FastAPI:
    """Mirror of app.main.app but without the lifespan."""
    test_app = FastAPI(title="cascade-test")
    test_app.include_router(health.router)
    test_app.include_router(snapshots.router)
    test_app.include_router(positions.router)

    @test_app.get("/")
    async def root():
        return {"name": "test", "docs": "/docs"}

    return test_app


@pytest.fixture
def client():
    fake = AsyncMongoMockClient()
    fake_db = fake["cascade_test"]

    # Repopulate the module-level singleton so get_db() doesn't raise
    mongo.client = fake  # type: ignore[assignment]
    mongo.db = fake_db  # type: ignore[assignment]

    test_app = _build_test_app()
    test_app.dependency_overrides[get_db] = lambda: fake_db

    with TestClient(test_app) as c:
        yield c

    test_app.dependency_overrides.clear()
    mongo.client = None
    mongo.db = None


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "docs" in r.json()


def test_snapshots_empty_returns_empty_list(client):
    r = client.get("/snapshots")
    assert r.status_code == 200
    assert r.json() == []


def test_latest_snapshot_404_when_no_data(client):
    r = client.get("/snapshots/latest")
    assert r.status_code == 404


def test_position_history_404_when_unknown(client):
    r = client.get("/positions/9999/history")
    assert r.status_code == 404


def test_at_risk_empty_when_no_snapshots(client):
    r = client.get("/positions/at-risk")
    assert r.status_code == 200
    assert r.json() == []


def test_snapshots_pagination_validation(client):
    # limit must be 1..200
    r = client.get("/snapshots?limit=500")
    assert r.status_code == 422


def test_at_risk_min_risk_validation(client):
    # Literal["MEDIUM","HIGH","CRITICAL"] — "low" is invalid
    r = client.get("/positions/at-risk?min_risk=low")
    assert r.status_code == 422
