"""Deploy LiquidationCascadeLogger to a Mantle network.

Usage:
    # 1. Set the deployer key + network in .env (or env vars):
    #      MANTLE_NETWORK=sepolia
    #      SEPOLIA_DEPLOYER_PRIVATE_KEY=0x...
    #      MANTLE_SEPOLIA_RPC=https://rpc.sepolia.mantle.xyz
    #
    # 2. Run from the project root:
    #      .venv/bin/python contracts/script/deploy.py
    #
    # 3. Paste the printed contract address into .env as CASCADE_LOGGER_ADDRESS

The script is idempotent on success — re-running deploys a fresh contract;
the previous one keeps working (immutable append-only log).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from solcx import compile_files, install_solc, set_solc_version
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

SOLC_VERSION = "0.8.20"
CONTRACT_PATH = ROOT / "contracts" / "src" / "LiquidationCascadeLogger.sol"
OUT_DIR = ROOT / "contracts" / "out"


def _ensure_solc() -> None:
    try:
        set_solc_version(SOLC_VERSION)
    except Exception:
        install_solc(SOLC_VERSION)
        set_solc_version(SOLC_VERSION)


def _compile() -> tuple[str, list[dict]]:
    _ensure_solc()
    output = compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi", "bin"],
        optimize=True,
        optimize_runs=200,
    )
    key = next(k for k in output if k.endswith(":LiquidationCascadeLogger"))
    bin_hex = output[key]["bin"]
    abi = output[key]["abi"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "LiquidationCascadeLogger.abi.json").write_text(json.dumps(abi, indent=2))
    (OUT_DIR / "LiquidationCascadeLogger.bin").write_text(bin_hex)
    print(f"  abi: {OUT_DIR / 'LiquidationCascadeLogger.abi.json'}")
    print(f"  bin: {OUT_DIR / 'LiquidationCascadeLogger.bin'} ({len(bin_hex) // 2} bytes)")
    return bin_hex, abi


def main() -> int:
    settings = get_settings()
    if not settings.sepolia_deployer_private_key:
        print("ERROR: SEPOLIA_DEPLOYER_PRIVATE_KEY not set in .env", file=sys.stderr)
        return 1

    print(f"Compiling {CONTRACT_PATH.name} with solc {SOLC_VERSION}...")
    bin_hex, abi = _compile()

    w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"ERROR: Cannot reach RPC at {settings.rpc_url}", file=sys.stderr)
        return 1
    print(f"Connected to network={settings.mantle_network} chain_id={w3.eth.chain_id}")

    acct = w3.eth.account.from_key(settings.sepolia_deployer_private_key)
    balance_wei = w3.eth.get_balance(acct.address)
    print(f"Deployer: {acct.address}")
    print(f"Balance:  {w3.from_wei(balance_wei, 'ether')} (native)")
    if balance_wei == 0:
        print(
            "ERROR: deployer balance is 0. Fund it from the Mantle Sepolia faucet:",
            "  https://faucet.sepolia.mantle.xyz",
            sep="\n",
            file=sys.stderr,
        )
        return 1

    contract = w3.eth.contract(abi=abi, bytecode=bin_hex)
    constructor_args = (acct.address,)  # agent = deployer

    print("Estimating gas...")
    gas_estimate = contract.constructor(*constructor_args).estimate_gas({"from": acct.address})
    gas_price = w3.eth.gas_price
    print(f"  gas_estimate={gas_estimate} gas_price={gas_price} "
          f"cost~={w3.from_wei(gas_estimate * gas_price, 'ether')}")

    print("Sending deploy transaction...")
    tx = contract.constructor(*constructor_args).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": int(gas_estimate * 1.2),
        "gasPrice": gas_price,
        "chainId": settings.chain_id,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  tx_hash={tx_hash.hex()}")

    print("Waiting for receipt...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        print("ERROR: deployment failed", file=sys.stderr)
        return 1

    print("\nDEPLOYED")
    print(f"  contract address: {receipt.contractAddress}")
    print(f"  block:            {receipt.blockNumber}")
    print(f"  gas_used:         {receipt.gasUsed}")
    print(f"  explorer:         {settings.explorer_base}/address/{receipt.contractAddress}")
    print("\nNext step: paste this in .env\n")
    print(f"  CASCADE_LOGGER_ADDRESS={receipt.contractAddress}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
