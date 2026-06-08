"""Centralized configuration.

Why Pydantic Settings instead of os.getenv() everywhere:
  - Type safety: int fields are int, not str
  - Validation at startup: bad config fails immediately, not at runtime
  - Single source of truth: every module imports `settings`, no scattered defaults
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Network = Literal["mainnet", "sepolia"]


@dataclass(frozen=True)
class InitCapitalAddresses:
    """INIT Capital contract addresses.

    Mainnet values are pulled from https://dev.init.capital/contract-addresses/mantle
    and verified. Sepolia values must be filled in when/if INIT deploys there;
    if empty, the app refuses to start on Sepolia (see validate() below).
    """

    init_core: str
    pos_manager: str
    config: str
    lending_pool: str
    risk_manager: str
    init_oracle: str
    liq_incentive_calculator: str

    def validate(self, network: str) -> None:
        """Raise loudly if any address is empty — better than mysterious RPC errors later."""
        missing = [name for name, val in vars(self).items() if not val.startswith("0x")]
        if missing:
            raise RuntimeError(
                f"INIT addresses missing for network={network}: {missing}. "
                "Set them in app/core/config.py or use MANTLE_NETWORK=mainnet."
            )


INIT_MAINNET = InitCapitalAddresses(
    init_core="0x972BcB0284cca0152527c4f70f8F689852bCAFc5",
    pos_manager="0x0e7401707CD08c03CDb53DAEF3295DDFb68BBa92",
    config="0x007F91636E0f986068Ef27c950FA18734BA553Ac",
    lending_pool="0x423bB7577BCf594df986D9646B44D3144b3329FD",
    risk_manager="0x0c03cd3e8b669680Bf306Fc72F1dc2cAC592f951",
    init_oracle="0x4E195A32b2f6eBa9c4565bA49bef34F23c2C0350",
    liq_incentive_calculator="0x66BDbf2Eefc84f83b476dB238574ca5Cb00550aD",
)

# Placeholder — INIT may not be deployed on Sepolia. Update if/when verified.
INIT_SEPOLIA = InitCapitalAddresses(
    init_core="",
    pos_manager="",
    config="",
    lending_pool="",
    risk_manager="",
    init_oracle="",
    liq_incentive_calculator="",
)


class Settings(BaseSettings):
    """All env-driven configuration in one typed object.

    SettingsConfigDict tells pydantic-settings WHERE to find env vars:
      - env_file: read .env at startup
      - case_sensitive=False: MANTLE_NETWORK == mantle_network
      - extra="ignore": unknown env vars are silently ignored (lets you share
        a .env across projects without crashes)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # Network
    mantle_network: Network = "mainnet"
    mantle_mainnet_rpc: str = "https://rpc.mantle.xyz"
    mantle_sepolia_rpc: str = "https://rpc.sepolia.mantle.xyz"

    # Polling
    poll_interval_seconds: int = 30
    max_positions_per_cycle: int = 0  # 0 = unlimited

    # --- Cascade detection ---
    # Health-factor threshold below which a position counts as at-risk.
    cascade_at_risk_hf: float = 1.15
    # Minimum cluster size to call something a "cascade zone".
    cascade_min_cluster_size: int = 3

    # --- Multi-protocol support ---
    # Toggle Lendle alongside INIT. Lendle adds Aave V2-style borrowers via
    # event discovery — independent of INIT's NFT enumeration.
    enable_lendle: bool = True
    # How many blocks back to scan for Lendle Borrow events on first cycle.
    # 50000 blocks ≈ 28 hours on Mantle. Larger windows = more borrowers
    # tracked but slower first poll.
    lendle_discovery_block_window: int = 50_000

    # --- Alerts ---
    discord_webhook_url: str = ""  # empty = alerts disabled, code still runs
    alert_dedup_window_seconds: int = 600

    # Telegram is a parallel channel — leave token blank to disable. Discord
    # and Telegram dedup share the same in-process state, so a deduped alert
    # is skipped on both channels (single source of truth per fingerprint).
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # channels usually negative, e.g. "-1001234567890"

    # --- Phase 4: on-chain logger ---
    sepolia_deployer_private_key: str = ""  # empty = on-chain logging disabled
    cascade_logger_address: str = ""  # set after deployment

    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "cascade"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # --- Derived properties ---

    @property
    def rpc_url(self) -> str:
        return self.mantle_mainnet_rpc if self.mantle_network == "mainnet" else self.mantle_sepolia_rpc

    @property
    def chain_id(self) -> int:
        return 5000 if self.mantle_network == "mainnet" else 5003

    @property
    def init_addresses(self) -> InitCapitalAddresses:
        return INIT_MAINNET if self.mantle_network == "mainnet" else INIT_SEPOLIA

    @property
    def explorer_base(self) -> str:
        return (
            "https://explorer.mantle.xyz"
            if self.mantle_network == "mainnet"
            else "https://explorer.sepolia.mantle.xyz"
        )


# --- Singleton accessor ---
# lru_cache turns this into a "load once, return same instance forever" pattern.
# FastAPI uses Depends(get_settings) so handlers can request settings via DI.
@lru_cache
def get_settings() -> Settings:
    return Settings()
