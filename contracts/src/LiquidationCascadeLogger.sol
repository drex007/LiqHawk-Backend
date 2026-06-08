// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title LiquidationCascadeLogger
/// @notice Append-only log of cascade predictions from the off-chain detector,
///         plus their on-chain-observed outcomes. Provides a public, tamper-
///         evident benchmark of detector accuracy.
///
/// Design notes:
///  - The agent (off-chain) is the only address allowed to call `logPrediction`.
///    The contract is single-tenant by design — multiple agents can deploy
///    their own loggers and prove their accuracy independently.
///  - `recordOutcome` is permissionless: anyone can submit the observed
///    cascade outcome for a prediction (gated by reasonableness checks).
///    This deliberately allows third-party audit — if our agent never calls
///    `recordOutcome`, others can.
///  - Probabilities and percentages are stored as basis points (1bp = 0.01%)
///    to keep ints; e.g. 8527 = 85.27%.
contract LiquidationCascadeLogger {
    // -------- Types -----------------------------------------------------

    enum Severity { LOW, MEDIUM, HIGH, CRITICAL }

    struct Prediction {
        uint64  timestamp;          // When predicted (seconds since epoch)
        uint64  blockNumber;        // Mantle block of the prediction
        uint16  probabilityBps;     // 0..10000 — predicted probability of cascade
        Severity severity;
        bytes32 collateralSymbolHash; // keccak256(symbol) — symbol stored off-chain
        uint32  numPositions;
        uint128 totalDebtUsd;       // USD, 6 decimals (i.e. 1_500_000_000_000 = $1.5M)
        uint16  timeToCascadeMin;   // Predicted minutes-to-cascade
        // Outcome — filled in by `recordOutcome` after the prediction window:
        bool    outcomeRecorded;
        bool    cascadeOccurred;
        uint32  actualLiquidations;
        uint128 actualLiquidatedUsd;
        uint64  outcomeBlockNumber;
        address outcomeReporter;
    }

    // -------- Storage ---------------------------------------------------

    address public immutable agent;

    Prediction[] private _predictions;

    // collateralSymbolHash → list of prediction IDs targeting that asset
    mapping(bytes32 => uint256[]) private _byCollateral;

    // -------- Events ----------------------------------------------------

    event PredictionLogged(
        uint256 indexed predictionId,
        bytes32 indexed collateralSymbolHash,
        Severity severity,
        uint16 probabilityBps,
        uint128 totalDebtUsd,
        uint64 timestamp,
        uint64 blockNumber
    );

    event OutcomeRecorded(
        uint256 indexed predictionId,
        bool cascadeOccurred,
        uint32 actualLiquidations,
        uint128 actualLiquidatedUsd,
        address indexed reporter
    );

    // -------- Errors ----------------------------------------------------

    error NotAgent();
    error UnknownPrediction();
    error AlreadyRecorded();
    error InvalidProbability();
    error PredictionWindowOpen();

    // -------- Constants -------------------------------------------------

    /// @notice Outcome window — predictions cannot be settled until at least
    /// this many seconds have elapsed since the prediction was logged. Keeps
    /// the agent honest: it can't predict and "confirm" in the same block.
    uint64 public constant OUTCOME_WINDOW = 30 minutes;

    // -------- Modifiers -------------------------------------------------

    modifier onlyAgent() {
        if (msg.sender != agent) revert NotAgent();
        _;
    }

    // -------- Constructor -----------------------------------------------

    constructor(address _agent) {
        agent = _agent == address(0) ? msg.sender : _agent;
    }

    // -------- Writes ----------------------------------------------------

    function logPrediction(
        uint16 probabilityBps,
        Severity severity,
        bytes32 collateralSymbolHash,
        uint32 numPositions,
        uint128 totalDebtUsd,
        uint16 timeToCascadeMin
    ) external onlyAgent returns (uint256 predictionId) {
        if (probabilityBps > 10_000) revert InvalidProbability();

        predictionId = _predictions.length;
        _predictions.push(
            Prediction({
                timestamp: uint64(block.timestamp),
                blockNumber: uint64(block.number),
                probabilityBps: probabilityBps,
                severity: severity,
                collateralSymbolHash: collateralSymbolHash,
                numPositions: numPositions,
                totalDebtUsd: totalDebtUsd,
                timeToCascadeMin: timeToCascadeMin,
                outcomeRecorded: false,
                cascadeOccurred: false,
                actualLiquidations: 0,
                actualLiquidatedUsd: 0,
                outcomeBlockNumber: 0,
                outcomeReporter: address(0)
            })
        );
        _byCollateral[collateralSymbolHash].push(predictionId);

        emit PredictionLogged(
            predictionId,
            collateralSymbolHash,
            severity,
            probabilityBps,
            totalDebtUsd,
            uint64(block.timestamp),
            uint64(block.number)
        );
    }

    /// @notice Permissionless. Records the cascade outcome for a prior
    /// prediction. Reverts if the outcome window hasn't elapsed (so no one
    /// can settle a prediction in the same block it was made).
    function recordOutcome(
        uint256 predictionId,
        bool cascadeOccurred,
        uint32 actualLiquidations,
        uint128 actualLiquidatedUsd
    ) external {
        if (predictionId >= _predictions.length) revert UnknownPrediction();
        Prediction storage p = _predictions[predictionId];
        if (p.outcomeRecorded) revert AlreadyRecorded();
        if (block.timestamp < p.timestamp + OUTCOME_WINDOW) {
            revert PredictionWindowOpen();
        }
        p.outcomeRecorded = true;
        p.cascadeOccurred = cascadeOccurred;
        p.actualLiquidations = actualLiquidations;
        p.actualLiquidatedUsd = actualLiquidatedUsd;
        p.outcomeBlockNumber = uint64(block.number);
        p.outcomeReporter = msg.sender;

        emit OutcomeRecorded(
            predictionId,
            cascadeOccurred,
            actualLiquidations,
            actualLiquidatedUsd,
            msg.sender
        );
    }

    // -------- Reads -----------------------------------------------------

    function predictionCount() external view returns (uint256) {
        return _predictions.length;
    }

    function getPrediction(uint256 predictionId)
        external
        view
        returns (Prediction memory)
    {
        if (predictionId >= _predictions.length) revert UnknownPrediction();
        return _predictions[predictionId];
    }

    function predictionsByCollateral(bytes32 collateralSymbolHash)
        external
        view
        returns (uint256[] memory)
    {
        return _byCollateral[collateralSymbolHash];
    }

    /// @notice Returns (correctPredictions, resolvedPredictions). A prediction
    /// counts as correct iff (severity >= MEDIUM AND cascade occurred) OR
    /// (severity == LOW AND cascade did NOT occur). Useful for an at-a-glance
    /// accuracy score; finer-grained scoring is left to off-chain analysis.
    function accuracy()
        external
        view
        returns (uint256 correct, uint256 resolved)
    {
        uint256 n = _predictions.length;
        for (uint256 i = 0; i < n; i++) {
            Prediction storage p = _predictions[i];
            if (!p.outcomeRecorded) continue;
            resolved++;
            bool predictedYes = uint8(p.severity) >= uint8(Severity.MEDIUM);
            if (predictedYes == p.cascadeOccurred) correct++;
        }
    }
}
