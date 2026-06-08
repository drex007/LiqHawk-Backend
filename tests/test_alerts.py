"""Alert publisher tests — no real Discord, no real network."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.schema import Position
from app.services.alerts import AlertPublisher
from app.services.cascade import detect_zones


class _FakeHTTPClient:
    """Captures POST payloads instead of sending them."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(204, request=httpx.Request("POST", url))

    async def aclose(self):
        pass


def _mk(pid: str, hf: str, sym: str = "mETH", coll="10000", debt="9000") -> Position:
    return Position(
        position_id=pid,
        owner=f"0x{int(pid):040x}",
        health_factor=Decimal(hf),
        total_collateral_usd=Decimal(coll),
        total_debt_usd=Decimal(debt),
        dominant_collateral=sym,
    )


@pytest.fixture
def fake_client():
    return _FakeHTTPClient()


@pytest.fixture
def publisher(fake_client):
    return AlertPublisher(
        webhook_url="https://discord.test/webhook/abc",
        http_client=fake_client,  # type: ignore[arg-type]
    )


async def test_publish_zone_sends_embed(publisher, fake_client):
    zones = detect_zones([_mk(str(i), "1.02") for i in range(3)])
    sent = await publisher.publish_cascade_zones(zones, block_number=100)
    assert sent == 1
    assert len(fake_client.posts) == 1
    url, payload = fake_client.posts[0]
    assert "embeds" in payload
    assert payload["embeds"][0]["title"].startswith("⚠️")


async def test_dedup_suppresses_repeated_zone(publisher, fake_client):
    zones = detect_zones([_mk(str(i), "1.02") for i in range(3)])
    await publisher.publish_cascade_zones(zones, block_number=100)
    # Second publish of the *same* zone — should dedup
    sent = await publisher.publish_cascade_zones(zones, block_number=101)
    assert sent == 0
    assert len(fake_client.posts) == 1


async def test_severity_escalation_defeats_dedup(publisher, fake_client):
    # First a MEDIUM zone (HF=1.10, only 3 positions)
    medium = detect_zones([_mk(str(i), "1.10") for i in range(3)])
    await publisher.publish_cascade_zones(medium, block_number=100)
    # Then the same collateral cluster but pushed to CRITICAL (HF=1.01)
    critical = detect_zones([_mk(str(i), "1.01") for i in range(10)])
    sent = await publisher.publish_cascade_zones(critical, block_number=101)
    assert sent == 1
    assert len(fake_client.posts) == 2


async def test_no_webhook_url_is_noop(fake_client):
    pub = AlertPublisher(webhook_url=None, http_client=fake_client)  # type: ignore[arg-type]
    zones = detect_zones([_mk(str(i), "1.02") for i in range(3)])
    sent = await pub.publish_cascade_zones(zones, block_number=100)
    assert sent == 1   # dedup admitted it
    assert len(fake_client.posts) == 0  # ...but nothing got POSTed


async def test_publish_new_critical_position(publisher, fake_client):
    p = _mk("1", "0.98")
    sent = await publisher.publish_new_critical_positions(
        previous={},  # never seen before
        current=[p],
        block_number=100,
    )
    assert sent == 1
    assert "CRITICAL" in fake_client.posts[0][1]["embeds"][0]["title"].upper()


async def test_already_critical_position_not_realerted(publisher, fake_client):
    p = _mk("1", "0.98")
    sent = await publisher.publish_new_critical_positions(
        previous={"1": "CRITICAL"},  # was critical last cycle too
        current=[p],
        block_number=100,
    )
    assert sent == 0


async def test_position_dedup_suppresses_within_window(publisher, fake_client):
    p = _mk("1", "0.98")
    await publisher.publish_new_critical_positions(
        previous={"1": "HIGH"},
        current=[p],
        block_number=100,
    )
    # Re-emit — fingerprint already in dedup set
    sent = await publisher.publish_new_critical_positions(
        previous={"1": "HIGH"},
        current=[p],
        block_number=101,
    )
    assert sent == 0


async def test_embed_color_matches_severity(publisher, fake_client):
    zones = detect_zones([_mk(str(i), "1.01") for i in range(10)])
    await publisher.publish_cascade_zones(zones, block_number=100)
    embed = fake_client.posts[0][1]["embeds"][0]
    # CRITICAL or HIGH should produce red or orange
    assert embed["color"] in (0xE74C3C, 0xE67E22, 0xF1C40F)
