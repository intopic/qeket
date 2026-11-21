import { createHash } from "node:crypto";
import { PublicKey } from "@solana/web3.js";

const tag = (name: string) => createHash("sha256").update("account:" + name).digest().subarray(0, 8);
const CONFIG_TAG = tag("GatekeeperConfig");
const PROPOSAL_TAG = tag("UpgradeProposal");
export const STATUSES = ["Pending", "Ready", "Executed", "Cancelled", "Reclaimed"] as const;
export type Status = (typeof STATUSES)[number];

export const sha256 = (data: Uint8Array) => createHash("sha256").update(data).digest();

/** The gate's `GatekeeperConfig` account, schema v2. */
export type GateConfig = {
  target: PublicKey;
  governance: PublicKey;
  emergency: PublicKey;
  replayMode: "StrictEquality" | "NoRevertAndCuBound";
  verifiers: PublicKey[];
  threshold: number;
  maxCuDeltaBps: number;
  minReplayTxs: number;
  minReplaySlots: bigint;
  timelockSeconds: bigint;
  proposalExpirySeconds: bigint;
  proposalNonce: bigint;
};

export function decodeConfig(data: Buffer): GateConfig {
  if (data.length !== 371 || !data.subarray(0, 8).equals(CONFIG_TAG) || data[8] !== 2) {
    throw new Error("not a v2 gate config account");
  }
  const count = data[331];
  if (count > 7 || data[106] > 1) throw new Error("invalid gate policy");
  const key = (start: number) => new PublicKey(data.subarray(start, start + 32));
  return {
    target: key(10),
    governance: key(42),
    emergency: key(74),
    replayMode: data[106] === 0 ? "StrictEquality" : "NoRevertAndCuBound",
    verifiers: Array.from({ length: count }, (_, i) => key(107 + 32 * i)),
    threshold: data[332],
    maxCuDeltaBps: data.readUInt16LE(333),
    minReplayTxs: data.readUInt32LE(335),
    minReplaySlots: data.readBigUInt64LE(339),
    timelockSeconds: data.readBigInt64LE(347),
    proposalExpirySeconds: data.readBigInt64LE(355),
    proposalNonce: data.readBigUInt64LE(363),
  };
}

/** The gate's `UpgradeProposal` account. */
export type GateProposal = {
  nonce: bigint;
  target: PublicKey;
  baselineDeploySlot: bigint;
  buffer: PublicKey;
  spill: PublicKey;
  elfSha256: Buffer;
  gitCommit: Buffer;
  anchorSlot: bigint;
  replaySlots: bigint;
  diffRoot: Buffer;
  votes: number;
  voteBitmap: number;
  createdAt: bigint;
  expiresAt: bigint;
  readyAt: bigint;
  status: Status;
};

export function decodeProposal(data: Buffer): GateProposal {
  if (data.length !== 248 || !data.subarray(0, 8).equals(PROPOSAL_TAG) || data[247] >= STATUSES.length) {
    throw new Error("not a gate upgrade proposal account");
  }
  return {
    nonce: data.readBigUInt64LE(9),
    target: new PublicKey(data.subarray(17, 49)),
    baselineDeploySlot: data.readBigUInt64LE(49),
    buffer: new PublicKey(data.subarray(57, 89)),
    spill: new PublicKey(data.subarray(89, 121)),
    elfSha256: Buffer.from(data.subarray(121, 153)),
    gitCommit: Buffer.from(data.subarray(153, 173)),
    anchorSlot: data.readBigUInt64LE(173),
    replaySlots: data.readBigUInt64LE(181),
    diffRoot: Buffer.from(data.subarray(189, 221)),
    voteBitmap: data[221],
    votes: data[222],
    createdAt: data.readBigInt64LE(223),
    expiresAt: data.readBigInt64LE(231),
    readyAt: data.readBigInt64LE(239),
    status: STATUSES[data[247]],
  };
}

/**
 * The gate accepts only a loader-v3 buffer whose authority is its config, so the
 * bytes cannot change after the proposal. They must be the program that was built.
 */
export function checkBuffer(data: Buffer, configAddress: PublicKey, elf: Buffer) {
  if (data.length < 37 || data.readUInt32LE(0) !== 1 || data[4] !== 1) {
    throw new Error("expected a loader-v3 buffer with an authority");
  }
  if (!new PublicKey(data.subarray(5, 37)).equals(configAddress)) {
    throw new Error(`the buffer authority must be the gate config ${configAddress.toBase58()}`);
  }
  const body = data.subarray(37);
  if (body.length < elf.length || !body.subarray(0, elf.length).equals(elf) || body.subarray(elf.length).some(b => b !== 0)) {
    throw new Error("the buffer does not hold the built program");
  }
}

/** Successful transactions of the target per slot, over its most recent history. */
export function trafficRate(signatures: { slot: number; err: unknown }[], currentSlot: number) {
  const ok = signatures.filter(entry => entry.err === null);
  if (ok.length === 0) return 0;
  const oldest = Math.min(...ok.map(entry => entry.slot));
  return ok.length / Math.max(1, currentSlot - oldest + 1);
}

/**
 * The replay window for a proposal. It must reach the gate's minimum slots and,
 * with headroom, its minimum transactions, while the expected traffic stays under
 * the plan's cap: a window with more transactions than the cap is refused.
 */
export function chooseWindow(input: {
  minReplaySlots: bigint; minReplayTxs: number; maxReplayTxs: number; txPerSlot: number;
}) {
  const { minReplaySlots, minReplayTxs, maxReplayTxs, txPerSlot } = input;
  if (!(txPerSlot > 0)) {
    throw new Error("the program has no recent successful transactions, so a replay window cannot be filled");
  }
  const needed = BigInt(Math.ceil((minReplayTxs * 1.5) / txPerSlot));
  const cap = BigInt(Math.floor((maxReplayTxs * 0.8) / txPerSlot));
  const slots = needed > minReplaySlots ? needed : minReplaySlots;
  if (slots > cap) {
    throw new Error(
      `at ${txPerSlot.toFixed(3)} transactions per slot, a window of ${slots} slots would exceed the plan's ` +
      `${maxReplayTxs} transactions. Lower min_replay_txs or min_replay_slots in the gate policy, or use a larger plan.`,
    );
  }
  return { replaySlots: slots, expectedTxs: Math.round(Number(slots) * txPerSlot) };
}

/** Whether `execute_gated_upgrade` would pass its time and status checks now. */
export function executionState(proposal: GateProposal, config: GateConfig, now: bigint) {
  if (proposal.status !== "Ready") return { ready: false, reason: `the proposal is ${proposal.status}` };
  if (now > proposal.expiresAt) return { ready: false, reason: "the proposal expired" };
  const earliest = proposal.readyAt + config.timelockSeconds;
  if (now < earliest) return { ready: false, reason: `the timelock ends in ${earliest - now} seconds` };
  return { ready: true, reason: "ready to execute" };
}

/** The latest proposal of the current policy generation, if any. */
export function latestNonce(config: GateConfig): bigint | null {
  return (config.proposalNonce & 0xffffffffn) === 0n ? null : config.proposalNonce - 1n;
}
