"""On-chain prediction logger.

Wraps the deployed `LiquidationCascadeLogger` contract. The pipeline calls
`log_zones()` once per poll cycle with the cascade zones produced by
`detect_zones()`; each zone becomes one on-chain `logPrediction` transaction.

When configured but unable to talk to chain (e.g., low balance, RPC down),
the logger logs a warning and otherwise stays out of the way — the off-chain
pipeline keeps working.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from eth_utils import keccak
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.middleware import ExtraDataToPOAMiddleware

from app.core.config import Settings
from app.core.logging_setup import get_logger
from app.services.cascade import CascadeZone, estimate_time_to_cascade_minutes

log = get_logger(__name__)

ABI_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts" / "out" / "LiquidationCascadeLogger.abi.json"
)

# Mirror Solidity enum order: LOW=0, MEDIUM=1, HIGH=2, CRITICAL=3
SEVERITY_ENUM = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

USD_SCALE_6_DECIMALS = Decimal(10) ** 6


class OnChainLogger:
    """Posts cascade predictions to the LiquidationCascadeLogger contract.

    Construction does NOT touch the network — it only loads the ABI and
    builds the contract handle. The first real call (log_zones) is when
    we hit RPC.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(
            settings.sepolia_deployer_private_key
            and settings.cascade_logger_address
        )
        if not self.enabled:
            return

        if not ABI_PATH.exists():
            raise FileNotFoundError(
                f"ABI not found at {ABI_PATH}. Run contracts/script/deploy.py first."
            )

        # Sepolia / mainnet use different RPC URLs. The logger lives on
        # *Sepolia* in our setup — Mantle Sepolia for cheap testnet logging.
        # The reader still runs against mainnet INIT.
        rpc_url = settings.mantle_sepolia_rpc
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.account = self.w3.eth.account.from_key(settings.sepolia_deployer_private_key)

        abi = json.loads(ABI_PATH.read_text())
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.cascade_logger_address),
            abi=abi,
        )

    async def log_zones(
        self, zones: Sequence[CascadeZone], *, block_number: int
    ) -> list[str]:
        """Submit one transaction per zone. Returns submitted tx hashes."""
        if not self.enabled:
            return []
        loop = asyncio.get_running_loop()
        # web3 is sync — offload to thread pool so it doesn't block.
        return await loop.run_in_executor(None, self._log_zones_sync, zones, block_number)

    def _log_zones_sync(
        self, zones: Sequence[CascadeZone], block_number: int
    ) -> list[str]:
        tx_hashes: list[str] = []
        try:
            nonce = self.w3.eth.get_transaction_count(self.account.address)
        except Exception as e:  # noqa: BLE001
            log.warning("on_chain_nonce_failed", err=str(e))
            return []

        for zone in zones:
            try:
                tx_hash = self._send_prediction(zone, nonce)
                tx_hashes.append(tx_hash)
                nonce += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "on_chain_log_zone_failed",
                    collateral=zone.collateral_symbol,
                    err=str(e),
                )
        if tx_hashes:
            log.info(
                "on_chain_zones_logged",
                count=len(tx_hashes),
                block_number=block_number,
            )
        return tx_hashes

    def _send_prediction(self, zone: CascadeZone, nonce: int) -> str:
        # Pack arguments to match the Solidity signature exactly.
        sym_hash = keccak(text=zone.collateral_symbol or "UNKNOWN")
        probability_bps = int(round(zone.probability * 10_000))
        severity = SEVERITY_ENUM.get(zone.severity, 0)
        num_positions = min(zone.num_positions, 2**32 - 1)
        # USD scaled to 6 decimals; truncate at uint128 max.
        debt_scaled = int((zone.total_debt_usd * USD_SCALE_6_DECIMALS).to_integral_value())
        debt_scaled = min(debt_scaled, 2**128 - 1)
        eta_min = min(estimate_time_to_cascade_minutes(zone), 2**16 - 1)

        fn = self.contract.functions.logPrediction(
            probability_bps,
            severity,
            sym_hash,
            num_positions,
            debt_scaled,
            eta_min,
        )

        try:
            gas_estimate = fn.estimate_gas({"from": self.account.address})
        except ContractLogicError as e:
            log.warning("on_chain_estimate_gas_failed", err=str(e))
            return ""

        gas_price = self.w3.eth.gas_price
        tx = fn.build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": gas_price,
            "chainId": 5003,  # Mantle Sepolia
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
