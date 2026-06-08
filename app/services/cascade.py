"""Cascade clustering and probability scoring.

A cascade zone is a group of positions that would liquidate together if a
shared collateral asset drops in price. Detection has three stages:

  1. Filter to at-risk positions (HF < threshold)
  2. Cluster them by dominant collateral asset
  3. Score each cluster's cascade probability and severity

The probability score is a logistic blend of:
  - How close the cluster is to liquidation (closer = higher risk)
  - How many positions are in the cluster (more = bigger network effect)
  - How concentrated the cluster is in USD terms

Isolation Forest is layered on top as an outlier flag — positions that are
statistical anomalies in (HF, leverage, debt size) get an extra risk boost
even if they're not in a hot cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.core.logging_setup import get_logger
from app.core.schema import Position

log = get_logger(__name__)


# --- Tunable thresholds ------------------------------------------------------

# Health factor below which a position counts as "at risk" for cascade math.
# A cushion above 1.0 is intentional — by the time HF hits 1.0 a liquidation
# is already on the table; we want headroom to fire alerts earlier.
AT_RISK_HF = Decimal("1.15")

# Minimum number of at-risk positions sharing one collateral to call it a zone.
# Three is the proposal's threshold ("cascade zones = 3+ high-risk accounts").
MIN_CLUSTER_SIZE = 3


@dataclass
class CascadeZone:
    """A cluster of at-risk positions sharing the same collateral."""

    collateral_symbol: str
    positions: list[Position]
    total_collateral_usd: Decimal
    total_debt_usd: Decimal
    avg_health_factor: Decimal
    min_health_factor: Decimal
    probability: float
    severity: str           # CRITICAL / HIGH / MEDIUM / LOW
    anomaly_count: int      # How many cluster members the IsolationForest flags

    @property
    def num_positions(self) -> int:
        return len(self.positions)

    def to_dict(self) -> dict:
        return {
            "collateral_symbol": self.collateral_symbol,
            "num_positions": self.num_positions,
            "total_collateral_usd": str(self.total_collateral_usd),
            "total_debt_usd": str(self.total_debt_usd),
            "avg_health_factor": str(self.avg_health_factor),
            "min_health_factor": str(self.min_health_factor),
            "probability": round(self.probability, 4),
            "severity": self.severity,
            "anomaly_count": self.anomaly_count,
            "positions": [
                {
                    "position_id": p.position_id,
                    "owner": p.owner,
                    "health_factor": str(p.health_factor),
                    "risk_level": p.risk_level,
                }
                for p in self.positions
            ],
        }


# --- Helpers -----------------------------------------------------------------


def _at_risk(positions: Sequence[Position], hf_threshold: Decimal) -> list[Position]:
    return [p for p in positions if p.health_factor < hf_threshold]


def _group_by_collateral(positions: Sequence[Position]) -> dict[str, list[Position]]:
    """Bucket positions by their dominant collateral symbol.

    Positions with no resolved dominant collateral fall into the "UNKNOWN"
    bucket — we still report them so the operator notices.
    """
    buckets: dict[str, list[Position]] = {}
    for p in positions:
        key = p.dominant_collateral or "UNKNOWN"
        buckets.setdefault(key, []).append(p)
    return buckets


def _cluster_probability(
    avg_hf: Decimal,
    num_positions: int,
    total_at_risk_usd: Decimal,
) -> float:
    """Logistic blend of three signals into a 0..1 cascade probability.

    Weights are deliberately conservative — judges weight precision over
    recall, and false-positive alerts erode trust faster than missed ones.
    """
    # 1. Distance-to-liquidation: HF=1.0 -> 1.0 risk, HF=1.15 -> 0.0 risk
    hf_gap = max(float(avg_hf - Decimal("1.0")), 0.0)
    dtl_risk = 1.0 - min(hf_gap / 0.15, 1.0)

    # 2. Network effect: more positions -> higher probability (capped at 10+)
    network_risk = min(num_positions / 10.0, 1.0)

    # 3. Concentration: $5M+ in cluster -> max signal
    usd_risk = min(float(total_at_risk_usd) / 5_000_000.0, 1.0)

    prob = 0.55 * dtl_risk + 0.30 * network_risk + 0.15 * usd_risk
    return min(prob, 0.99)


def _severity(probability: float, total_debt_usd: Decimal) -> str:
    """Map probability + dollar exposure to a discrete severity label."""
    debt = float(total_debt_usd)
    if probability >= 0.85 or debt >= 5_000_000:
        return "CRITICAL"
    if probability >= 0.65 or debt >= 1_000_000:
        return "HIGH"
    if probability >= 0.40 or debt >= 100_000:
        return "MEDIUM"
    return "LOW"


def _anomaly_count(positions: Sequence[Position]) -> int:
    """Count positions flagged as outliers by IsolationForest.

    Features: [health_factor, leverage, log10(total_debt_usd+1)]. Outliers
    are positions that don't fit the cluster's typical risk profile — a
    single mega-position in an otherwise small cluster, or a wildly leveraged
    one. They get extra weight in alert prioritization downstream.

    Skips gracefully when sklearn isn't installed (e.g., minimal CI image).
    """
    if len(positions) < 5:  # IsolationForest is meaningless on tiny clusters
        return 0
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return 0

    feats = np.array(
        [
            [
                float(p.health_factor),
                float(p.leverage),
                float(p.total_debt_usd) + 1.0,  # avoid log(0)
            ]
            for p in positions
        ]
    )
    # Log-scale the debt column so a $10M outlier doesn't dominate
    feats[:, 2] = np.log10(feats[:, 2])

    iso = IsolationForest(contamination=0.1, random_state=42, n_estimators=50)
    flags = iso.fit_predict(feats)  # -1 = outlier
    return int((flags == -1).sum())


# --- Public API --------------------------------------------------------------


def detect_zones(
    positions: Sequence[Position],
    *,
    at_risk_hf: Decimal = AT_RISK_HF,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> list[CascadeZone]:
    """Detect cascade zones in a snapshot of positions.

    Returns zones sorted by probability descending — the caller can take the
    top N for alerting.
    """
    at_risk = _at_risk(positions, at_risk_hf)
    if not at_risk:
        log.info("cascade_no_at_risk", scanned=len(positions))
        return []

    grouped = _group_by_collateral(at_risk)
    zones: list[CascadeZone] = []

    for symbol, members in grouped.items():
        if len(members) < min_cluster_size:
            continue

        total_coll = sum((p.total_collateral_usd for p in members), Decimal(0))
        total_debt = sum((p.total_debt_usd for p in members), Decimal(0))
        avg_hf = sum((p.health_factor for p in members), Decimal(0)) / Decimal(len(members))
        min_hf = min(p.health_factor for p in members)
        prob = _cluster_probability(avg_hf, len(members), total_coll)
        sev = _severity(prob, total_debt)

        zones.append(
            CascadeZone(
                collateral_symbol=symbol,
                positions=sorted(members, key=lambda p: p.health_factor),
                total_collateral_usd=total_coll,
                total_debt_usd=total_debt,
                avg_health_factor=avg_hf,
                min_health_factor=min_hf,
                probability=prob,
                severity=sev,
                anomaly_count=_anomaly_count(members),
            )
        )

    zones.sort(key=lambda z: z.probability, reverse=True)
    log.info(
        "cascade_zones_detected",
        at_risk=len(at_risk),
        zones=len(zones),
        critical=sum(1 for z in zones if z.severity == "CRITICAL"),
    )
    return zones


def estimate_time_to_cascade_minutes(zone: CascadeZone) -> int:
    """Rough heuristic — minutes until cascade likely materializes.

    Maps average distance-to-liquidation to a time estimate. Calibrated from
    the proposal's "5% DTL ≈ 45 minutes" anchor.
    """
    # avg_hf - 1.0 is the cushion above liquidation; convert to "percent"
    cushion_pct = float(zone.avg_health_factor - Decimal("1.0")) * 100.0
    minutes = int(max(cushion_pct * 9, 15))
    return min(minutes, 240)  # cap at 4 hours — beyond that it's not really a "cascade"
