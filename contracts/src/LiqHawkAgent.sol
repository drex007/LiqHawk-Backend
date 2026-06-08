// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title LiqHawkAgent — ERC-8004-style on-chain agent identity
/// @notice Single-token agent identity that binds the off-chain LiqHawk
///         detector to a permanent, queryable on-chain record. Anyone can
///         look up the agent's name, capabilities, operator, and the
///         contract holding its prediction history without trusting any
///         centralised service.
///
/// Design notes:
///  - Inspired by the ERC-8004 draft (trustless agents). Kept minimal: one
///    identity, public metadata, and a permissionless attestation log.
///    Not strictly ERC-721 to keep bytecode small for the hackathon — the
///    practical surface (name, capabilities, loggerContract, attestations)
///    is what judges and integrators actually read.
///  - The agent itself is identified by `operator` (an EOA or contract).
///    Transferring identity = redeploy with the new operator; we keep a
///    pointer to the previous identity for migration trails.
///  - Attestations are permissionless and append-only. The `attester`
///    address is recorded so third parties can build trust webs off-chain.
contract LiqHawkAgent {
    // -------- Types ---------------------------------------------------------

    struct AgentMetadata {
        address operator;            // The EOA or contract running the agent
        string  name;                // e.g. "LiqHawk"
        string  description;         // One-line pitch
        string  version;             // Semantic version: "0.5.0"
        string[] capabilities;       // ["liquidation-cascade-detection", ...]
        address loggerContract;      // → LiquidationCascadeLogger (accuracy())
        address previousIdentity;    // Optional: prior LiqHawkAgent contract
        uint64  createdAt;           // Block timestamp at deployment
    }

    struct Attestation {
        address attester;            // Who made the attestation
        bytes32 topicHash;           // keccak256 of attestation topic
        string  note;                // Free-text claim (truncate off-chain)
        uint64  timestamp;
    }

    // -------- Storage -------------------------------------------------------

    AgentMetadata private _agent;
    Attestation[] private _attestations;

    // -------- Events --------------------------------------------------------

    event AgentMinted(address indexed operator, string name, string version);
    event AttestationRecorded(
        uint256 indexed attestationId,
        address indexed attester,
        bytes32 indexed topicHash
    );

    // -------- Errors --------------------------------------------------------

    error NotOperator();
    error UnknownAttestation();

    // -------- Constructor ---------------------------------------------------

    constructor(
        string memory name_,
        string memory description_,
        string memory version_,
        string[] memory capabilities_,
        address loggerContract_,
        address previousIdentity_
    ) {
        _agent = AgentMetadata({
            operator: msg.sender,
            name: name_,
            description: description_,
            version: version_,
            capabilities: capabilities_,
            loggerContract: loggerContract_,
            previousIdentity: previousIdentity_,
            createdAt: uint64(block.timestamp)
        });
        emit AgentMinted(msg.sender, name_, version_);
    }

    // -------- Modifiers -----------------------------------------------------

    modifier onlyOperator() {
        if (msg.sender != _agent.operator) revert NotOperator();
        _;
    }

    // -------- Reads ---------------------------------------------------------

    function agent() external view returns (AgentMetadata memory) {
        return _agent;
    }

    function operator() external view returns (address) {
        return _agent.operator;
    }

    function name() external view returns (string memory) {
        return _agent.name;
    }

    function loggerContract() external view returns (address) {
        return _agent.loggerContract;
    }

    function capabilities() external view returns (string[] memory) {
        return _agent.capabilities;
    }

    function attestationCount() external view returns (uint256) {
        return _attestations.length;
    }

    function getAttestation(uint256 id) external view returns (Attestation memory) {
        if (id >= _attestations.length) revert UnknownAttestation();
        return _attestations[id];
    }

    // -------- Writes --------------------------------------------------------

    /// @notice Permissionless. Anyone can attest to a claim about this agent
    /// (e.g., "I verified prediction #42 came true"). Attestations are
    /// append-only and tagged with msg.sender so off-chain consumers can
    /// weight them by reputation, multisig membership, etc.
    function attest(bytes32 topicHash, string calldata note)
        external
        returns (uint256 attestationId)
    {
        attestationId = _attestations.length;
        _attestations.push(Attestation({
            attester: msg.sender,
            topicHash: topicHash,
            note: note,
            timestamp: uint64(block.timestamp)
        }));
        emit AttestationRecorded(attestationId, msg.sender, topicHash);
    }

    /// @notice Operator can update the version string after deployment
    /// (e.g., bumping when shipping a new release). Capabilities and logger
    /// are immutable — those define the agent's identity and shouldn't drift.
    function setVersion(string calldata newVersion) external onlyOperator {
        _agent.version = newVersion;
    }
}
