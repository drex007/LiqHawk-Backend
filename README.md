# Liquidation Cascade Detector

FastAPI + MongoDB service that monitors INIT Capital on Mantle for liquidation risk. Built for the Mantle Turing Test Hackathon 2026 — AI Alpha & Data track.

---

## Project layout

```
cascade-detector/
├── README.md                      ← you are here
├── pyproject.toml                 ← dependencies + tool config
├── .env.example                   ← env template (copy to .env)
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                    ← FastAPI app + lifespan startup/shutdown
│   │
│   ├── core/                      ← cross-cutting concerns
│   │   ├── __init__.py
│   │   ├── config.py              ← Settings, INIT Capital addresses
│   │   ├── schema.py              ← Pydantic models (Position, Snapshot, API)
│   │   └── logging_setup.py       ← structlog JSON logging
│   │
│   ├── db/                        ← MongoDB layer
│   │   ├── __init__.py
│   │   ├── mongo.py               ← motor async connection + indexes
│   │   └── repositories.py        ← ALL Mongo queries live here
│   │
│   ├── services/                  ← business logic, blockchain reads
│   │   ├── __init__.py
│   │   ├── mantle_client.py       ← web3.py wrapper + retry
│   │   ├── init_capital.py        ← INIT contract reader
│   │   └── pipeline.py            ← background polling task
│   │
│   └── api/                       ← HTTP routes (thin handlers)
│       ├── __init__.py
│       ├── health.py              ← /health, /diagnostics/snapshot
│       ├── snapshots.py           ← /snapshots, /snapshots/latest
│       └── positions.py           ← /positions/at-risk, /positions/{id}/history
│
└── tests/
    ├── __init__.py
    ├── test_schema.py             ← pure-Python risk math (7 tests)
    └── test_api.py                ← HTTP routes with in-memory Mongo (7 tests)
```

---

## Prerequisites

| Tool   | Version | How to check       |
| ------ | ------- | ------------------ |
| Python | 3.11+   | `python --version` |
| Docker | any     | `docker --version` |
| git    | any     | `git --version`    |

Docker is only required if you don't already have MongoDB. If you have MongoDB installed locally, skip the Docker step.

---

## Setup — first time

### 1. Get the code into a directory

```bash
# unzip cascade-detector.zip → cd into it
cd cascade-detector
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
# OR
.venv\Scripts\activate             # Windows PowerShell
```

You should see `(.venv)` in your prompt.

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -e ".[ml,dev]"
```

The `-e` flag installs the project in editable mode — code changes take effect without reinstalling. `[ml,dev]` pulls the optional ML libraries (numpy, scikit-learn) and dev tools (pytest, ruff).

### 4. Copy the env template

```bash
cp .env.example .env
```

Default values work for mainnet read-only. No editing required to start.

### 5. Start MongoDB

If you don't already have Mongo running:

```bash
docker run -d -p 27017:27017 --name cascade-mongo mongo:7
```

Verify it's up:

```bash
docker ps | grep cascade-mongo
```

To stop later: `docker stop cascade-mongo`. To remove: `docker rm cascade-mongo`.

### 6. Run the tests to confirm everything works

```bash
pytest
```

Expected: **14 passed**. No MongoDB or RPC connection needed — tests use in-memory mocks.

---

## Running the app

```bash
  uvicorn app.main:app --reload
```

`--reload` watches for code changes and restarts. Drop it in production.

You should see structured JSON logs like:

```json
{"event": "app_starting", "network": "mainnet", ...}
{"event": "mongo_connected", "uri": "mongodb://localhost:27017", ...}
{"event": "mongo_indexes_ready", ...}
{"event": "rpc_connected", "chain_id": 5000, ...}
{"event": "poller_started", "interval": 30, ...}
```

If you see those five lines, the app is live and polling.

The server listens on `http://localhost:8000`.

---

## What to hit first

### 1. Swagger UI (the easiest way)

Open in your browser:

```
http://localhost:8000/docs
```

Every endpoint with a "Try it out" button. No curl needed.

### 2. Health check

```bash
curl http://localhost:8000/health
```

Expect:

```json
{
  "status": "ok",
  "network": "mainnet",
  "chain_id": 5000,
  "latest_block": null,
  "mongo_connected": true
}
```

`latest_block` will be `null` until the first poll cycle completes (up to 30 seconds after startup).

### 3. Force a snapshot RIGHT NOW (the smoke test)

```bash
curl -X POST http://localhost:8000/diagnostics/snapshot
```

This is the critical first call. It bypasses the polling interval and reads INIT Capital directly. **If the ABI fragments in `app/services/init_capital.py` don't match the live contract, this surfaces the error immediately.**

Expected on success:

```json
{
  "block_number": 73824591,
  "captured_at": "2026-05-16T19:42:11Z",
  "total_positions": 142,
  "at_risk_count": 7,
  "sample": [
    {"position_id": 1043, "owner": "0x...", "health_factor": "0.9871", "risk_level": "CRITICAL"},
    ...
  ]
}
```

If you get an error here, paste it back to me — usually means the ABI selectors need adjusting.

### 4. Read the latest snapshot

```bash
curl http://localhost:8000/snapshots/latest | jq
```

### 5. Currently at-risk positions

```bash
curl "http://localhost:8000/positions/at-risk?min_risk=HIGH" | jq
```

### 6. Position history (after a few snapshots have accumulated)

```bash
curl http://localhost:8000/positions/1043/history | jq
```

---

## Switching networks

Edit `.env`:

```bash
MANTLE_NETWORK=mainnet   # default — real positions, read-only
MANTLE_NETWORK=sepolia   # for the Solidity logger phase
```

Restart the server. That's it.

⚠️ Note: Sepolia placeholder addresses in `app/core/config.py` are empty by default. The app will refuse to start on Sepolia until they're filled in. This is intentional — better than reading garbage data.

---

## Common issues

| Symptom                                                       | Fix                                                                                             |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `pymongo.errors.ServerSelectionTimeoutError`                  | MongoDB isn't running. `docker ps` to check. Restart: `docker start cascade-mongo`              |
| `ConnectionError: Cannot reach RPC at https://rpc.mantle.xyz` | Public RPC rate-limited you. Get a free Alchemy key, paste in `.env` under `MANTLE_MAINNET_RPC` |
| `totalSupply call failed` in /diagnostics/snapshot            | INIT's PosManager doesn't use ERC721Enumerable. Patch needed — share the error.                 |
| `credits_read_failed` warnings in logs                        | INIT credit function selectors differ. Patch needed — share the error.                          |
| Port 8000 already in use                                      | `uvicorn app.main:app --port 8001` or kill whatever's holding 8000                              |

---

## Development workflow

```bash
# Run tests
pytest

# Run tests in watch mode (re-run on save)
pytest --watch     # requires pytest-watch (pip install pytest-watch)

# Format/lint
ruff format .
ruff check . --fix
```

---

## Status

- [x] Phase 1: FastAPI + Mongo data pipeline
- [x] Phase 1.2: INIT ABI verified against live mainnet (`/diagnostics/snapshot` confirms)
- [x] Phase 1.3: Per-leg collateral/debt + pool→underlying symbol resolution
- [x] Phase 2: Cascade clustering — `detect_zones()` + `GET /cascade/zones`
- [x] Phase 3: Discord alert publisher (webhook + dedup)
- [x] Phase 4: `LiquidationCascadeLogger.sol` + Python on-chain logger
- [x] Phase 5: **Multi-protocol** — INIT + Lendle (Aave V2 fork) running side-by-side
- [ ] Phase 6: Backtesting framework
- [ ] Phase 7: Live dashboard (React frontend, optional)

---

## Protocols monitored

| Protocol | Type | Discovery model | Status |
| --- | --- | --- | --- |
| **INIT Capital** | Position-level NFT lender | `totalSupply` + `tokenByIndex` (ERC721 Enumerable) | live |
| **Lendle** | Aave V2 fork, account-level | Event scraping (`Borrow` logs) + `getUserAccountData` | live |

Positions from both protocols feed into the same snapshot and the same cascade detector — clustering groups them by **dominant collateral symbol** regardless of which protocol they came from. A cascade zone of mETH positions can span both INIT and Lendle when the underlying drops.

Toggle Lendle on/off via `.env`:

```bash
ENABLE_LENDLE=true                       # default
LENDLE_DISCOVERY_BLOCK_WINDOW=50000      # blocks to scan for borrowers (~28h on Mantle)
```

---

## Endpoints (current)

| Route | Notes |
| --- | --- |
| `GET /health` | Liveness — pings Mongo + reports latest block |
| `POST /diagnostics/snapshot` | Force one snapshot read now (the smoke test) |
| `GET /snapshots` | Paginated snapshot list (summary) |
| `GET /snapshots/latest` | Latest snapshot, fully hydrated with positions |
| `GET /positions/at-risk` | Positions at or above a given risk level |
| `GET /positions/{position_id}/history` | History for one position. **`position_id` is a string** (INIT NFT IDs are uint256, ~77 digits) |
| `GET /cascade/zones` | Cascade clusters detected in the latest snapshot |

---

## Discord alerts

Drop a webhook URL into `.env`:

```bash
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Restart the app. Two kinds of alerts will fire:

- **Cascade zone alert** — when ≥3 positions sharing the same collateral cluster near liquidation. Severity (LOW/MEDIUM/HIGH/CRITICAL), probability, expected debt, ETA in minutes.
- **CRITICAL position alert** — when an individual position crosses HF < 1.0 since the previous snapshot.

Both kinds are deduplicated in-process for `ALERT_DEDUP_WINDOW_SECONDS` (default 10 min). Severity escalation (HIGH → CRITICAL) defeats dedup and re-alerts.

If `DISCORD_WEBHOOK_URL` is empty, the pipeline still runs — alerts are just logged, not posted.

---

## On-chain prediction logger (Mantle Sepolia)

The contract at `contracts/src/LiquidationCascadeLogger.sol` is an append-only log of cascade predictions + their later-observed outcomes. Anyone can call `accuracy()` to read the agent's track record.

### Deploy

1. Get test MNT from [Mantle Sepolia faucet](https://faucet.sepolia.mantle.xyz).
2. Add to `.env`:
   ```bash
   MANTLE_NETWORK=sepolia
   SEPOLIA_DEPLOYER_PRIVATE_KEY=0x...  # throwaway key, never your real one
   ```
3. Run:
   ```bash
   .venv/bin/python contracts/script/deploy.py
   ```
4. Paste the printed address into `.env`:
   ```bash
   CASCADE_LOGGER_ADDRESS=0x...
   ```
5. Set `MANTLE_NETWORK=mainnet` again — the reader watches mainnet INIT, but the logger writes to Sepolia. Restart the app.

The poller will now submit one `logPrediction` tx per cascade zone per cycle. Failures are logged and don't block the pipeline.
