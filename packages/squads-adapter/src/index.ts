import { createHash } from "node:crypto";
import {
  AccountMeta, PublicKey, SYSVAR_CLOCK_PUBKEY, SYSVAR_RENT_PUBKEY,
  SystemProgram, TransactionInstruction, TransactionMessage,
} from "@solana/web3.js";
import { getVaultPda, instructions, PROGRAM_ID } from "@sqds/multisig";

const loader = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");

type Context = { gate: PublicKey; target: PublicKey };
type Upgrade = Context & { nonce: bigint; buffer: PublicKey; spill: PublicKey };
type Vault = { multisigPda: PublicKey; vaultIndex: number; squadsProgram?: PublicKey };
type Policy = {
  verifiers: PublicKey[];
  threshold: number;
  maxCuDeltaBps: number;
  minReplayTxs: number;
  minReplaySlots: bigint;
  timelockSeconds: bigint;
  proposalExpirySeconds: bigint;
  replayMode: "StrictEquality" | "NoRevertAndCuBound";
};

const ro = (pubkey: PublicKey, isSigner = false): AccountMeta => ({ pubkey, isSigner, isWritable: false });
const rw = (pubkey: PublicKey, isSigner = false): AccountMeta => ({ pubkey, isSigner, isWritable: true });
const u64 = (value: bigint) => {
  const data = Buffer.alloc(8);
  data.writeBigUInt64LE(value);
  return data;
};
const derive = (program: PublicKey, ...seeds: Buffer[]) => PublicKey.findProgramAddressSync(seeds, program)[0];
const config = ({gate, target}: Context) => derive(gate, Buffer.from("gatekeeper_cfg"), target.toBuffer());
const programData = (target: PublicKey) => derive(loader, target.toBuffer());
const proposal = ({gate, target, nonce}: Context & { nonce: bigint }) =>
  derive(gate, Buffer.from("proposal"), target.toBuffer(), u64(nonce));
const reservation = (input: Context & { buffer: PublicKey }) =>
  derive(input.gate, Buffer.from("buffer"), config(input).toBuffer(), input.buffer.toBuffer());
const pendingPolicy = (input: Context) => derive(input.gate, Buffer.from("policy"), config(input).toBuffer());
const pendingAuthority = (input: Context) => derive(input.gate, Buffer.from("authority"), config(input).toBuffer());

/** Pending policy and authority changes refund their rent to the payer that queued them, the vault by default. */
type Refund = { refund?: PublicKey };
const ROLES = { Governance: 0, Emergency: 1, ProgramUpgrade: 2 } as const;

/** Borsh layout of the gate's `Policy`. */
function encodePolicy(policy: Policy) {
  if (policy.replayMode !== "StrictEquality" && policy.replayMode !== "NoRevertAndCuBound") {
    throw new Error("invalid replay mode");
  }
  if (policy.verifiers.length > 7 || !Number.isInteger(policy.threshold) || policy.threshold < 1 ||
      policy.threshold > policy.verifiers.length || new Set(policy.verifiers.map(k => k.toBase58())).size !== policy.verifiers.length) {
    throw new Error("invalid verifier quorum");
  }
  const fields = Buffer.alloc(32);
  fields.writeUInt8(policy.verifiers.length, 0);
  fields.writeUInt8(policy.threshold, 1);
  fields.writeUInt16LE(policy.maxCuDeltaBps, 2);
  fields.writeUInt32LE(policy.minReplayTxs, 4);
  fields.writeBigUInt64LE(policy.minReplaySlots, 8);
  fields.writeBigInt64LE(policy.timelockSeconds, 16);
  fields.writeBigInt64LE(policy.proposalExpirySeconds, 24);
  const verifierKeys = Buffer.alloc(7 * 32);
  policy.verifiers.forEach((key, index) => key.toBuffer().copy(verifierKeys, index * 32));
  return Buffer.concat([Buffer.of(policy.replayMode === "StrictEquality" ? 0 : 1), verifierKeys, fields]);
}

function ix(gate: PublicKey, name: string, keys: AccountMeta[], args = Buffer.alloc(0)) {
  const tag = createHash("sha256").update("global:" + name).digest().subarray(0, 8);
  return new TransactionInstruction({ programId: gate, keys, data: Buffer.concat([tag, args]) });
}

function vault(input: Vault) {
  return getVaultPda({ multisigPda: input.multisigPda, index: input.vaultIndex, programId: input.squadsProgram ?? PROGRAM_ID })[0];
}

export * from "./gate.js";
export { loader as LOADER };
/** The gate config of a target. It must hold the program's and the buffer's upgrade authority. */
export const configAddress = (input: Context) => config(input);
export const proposalAddress = (input: Context & { nonce: bigint }) => proposal(input);
export const vaultAddress = (input: Vault) => vault(input);

export function initializeGatekeeper(input: Context & Vault & { emergency: PublicKey; policy: Policy }) {
  const { gate, target, emergency, policy } = input;
  const governance = vault(input);
  const encoded = encodePolicy(policy);
  if (governance.equals(emergency) || emergency.equals(PublicKey.default)) throw new Error("invalid emergency authority");
  const data = Buffer.concat([encoded, emergency.toBuffer()]);
  return ix(gate, "initialize_gatekeeper", [
    rw(governance, true), ro(governance, true), ro(target), ro(programData(target)),
    rw(config(input)), ro(SystemProgram.programId),
  ], data);
}

export function proposeBufferUpgrade(input: Upgrade & Vault & {
  elfSha256: Uint8Array; gitCommit: Uint8Array; anchorSlot?: bigint; replaySlots: bigint;
}) {
  const {gate, target, buffer, spill, elfSha256, gitCommit, anchorSlot, replaySlots} = input;
  if (elfSha256.length !== 32 || gitCommit.length !== 20) throw new Error("expected SHA-256 and 20-byte Git commit");
  const governance = vault(input);
  const anchor = anchorSlot === undefined ? Buffer.of(0) : Buffer.concat([Buffer.of(1), u64(anchorSlot)]);
  return ix(gate, "propose_buffer_upgrade", [
    rw(governance, true), ro(governance, true), rw(config(input)), ro(programData(target)),
    ro(buffer), rw(proposal(input)), rw(reservation(input)), ro(SystemProgram.programId),
  ], Buffer.concat([spill.toBuffer(), elfSha256, gitCommit, anchor, u64(replaySlots)]));
}

/**
 * Return the rent of a buffer that was handed to the gate but never proposed, for example
 * after a failed CI run. A proposed buffer is released only through `reclaimBuffer`.
 */
export function reclaimOrphanBuffer(input: Context & Vault & { buffer: PublicKey; spill: PublicKey }) {
  return ix(input.gate, "reclaim_orphan_buffer", [
    ro(vault(input), true), ro(config(input)), rw(input.buffer), ro(reservation(input)), rw(input.spill), ro(loader),
  ]);
}

/** Governance withdraws a pending or ready proposal. `reclaimBuffer` then returns the buffer's rent. */
export function cancelProposal(input: Context & Vault & { nonce: bigint }) {
  return ix(input.gate, "cancel_proposal", [ro(vault(input), true), ro(config(input)), rw(proposal(input))]);
}

/** Close an executed or reclaimed proposal and its buffer reservation. The rent goes to the proposal's spill account. */
export function closeProposal(input: Upgrade & Vault) {
  return ix(input.gate, "close_proposal", [
    ro(vault(input), true), ro(config(input)), rw(proposal(input)), rw(reservation(input)), rw(input.spill),
  ]);
}

/**
 * Queue a policy change, for example between `StrictEquality` and `NoRevertAndCuBound`.
 * `executeConfigUpdate` applies it once the current timelock passed. Applying it invalidates open proposals.
 */
export function proposeConfigUpdate(input: Context & Vault & { policy: Policy }) {
  const governance = vault(input);
  return ix(input.gate, "propose_config_update", [
    rw(governance, true), ro(governance, true), ro(config(input)), rw(pendingPolicy(input)), ro(SystemProgram.programId),
  ], encodePolicy(input.policy));
}

export function executeConfigUpdate(input: Context & Vault & Refund) {
  return resolvePending(input, "execute_config_update", pendingPolicy(input));
}

export function cancelConfigUpdate(input: Context & Vault & Refund) {
  return resolvePending(input, "cancel_config_update", pendingPolicy(input));
}

function resolvePending(input: Context & Vault & Refund, name: string, pending: PublicKey, extra: AccountMeta[] = []) {
  const governance = vault(input);
  return ix(input.gate, name, [
    ro(governance, true), rw(config(input)), rw(pending), rw(input.refund ?? governance), ...extra,
  ]);
}

/**
 * Upgrade without votes or timelock. Only the emergency authority set at initialization signs. When that
 * authority is a Squads vault, wrap the instruction with `createVaultTransaction` for its multisig. The buffer's
 * authority must be the gate config.
 */
export function emergencyOverrideUpgrade(input: Context & { emergency: PublicKey; buffer: PublicKey; spill: PublicKey }) {
  return ix(input.gate, "emergency_override_upgrade", [
    ro(input.emergency, true), ro(config(input)), rw(input.target), rw(programData(input.target)),
    rw(input.buffer), rw(input.spill), ro(SYSVAR_RENT_PUBKEY), ro(SYSVAR_CLOCK_PUBKEY), ro(loader),
  ]);
}

export function executeGatedUpgrade(input: Upgrade) {
  const {gate, target, buffer, spill} = input;
  return ix(gate, "execute_gated_upgrade", [
    ro(config(input)), rw(proposal(input)), rw(target), rw(programData(target)), rw(buffer), rw(spill),
    ro(SYSVAR_RENT_PUBKEY), ro(SYSVAR_CLOCK_PUBKEY), ro(loader),
  ]);
}

export function reclaimBuffer(input: Upgrade & Vault) {
  return ix(input.gate, "reclaim_buffer", [
    ro(vault(input), true), ro(config(input)), rw(proposal(input)), rw(input.buffer), rw(input.spill), ro(loader),
  ]);
}

/** Queue a new governance, emergency or program-upgrade authority. It applies after the timelock. */
export function proposeAuthorityChange(input: Context & Vault & { role: keyof typeof ROLES; candidate: PublicKey }) {
  if (!(input.role in ROLES)) throw new Error("invalid authority role");
  if (input.candidate.equals(PublicKey.default)) throw new Error("invalid authority candidate");
  const governance = vault(input);
  return ix(input.gate, "propose_authority_change", [
    rw(governance, true), ro(governance, true), ro(config(input)), rw(pendingAuthority(input)), ro(SystemProgram.programId),
  ], Buffer.concat([Buffer.of(ROLES[input.role]), input.candidate.toBuffer()]));
}

export function proposeProgramAuthorityTransfer(input: Context & Vault & {candidate: PublicKey}) {
  return proposeAuthorityChange({ ...input, role: "ProgramUpgrade" });
}

/** Apply a queued governance or emergency authority change. The candidate signs too. */
export function executeAuthorityChange(input: Context & Vault & Refund & { candidate: PublicKey }) {
  return resolvePending(input, "execute_authority_change", pendingAuthority(input), [ro(input.candidate, true)]);
}

export function cancelAuthorityChange(input: Context & Vault & Refund) {
  return resolvePending(input, "cancel_authority_change", pendingAuthority(input));
}

export function transferProgramAuthority(input: Context & Vault & {candidate: PublicKey; emergency?: boolean}) {
  const cfg = config(input);
  const pending = input.emergency ? input.gate : pendingAuthority(input);
  return ix(input.gate, "transfer_program_authority", [
    ro(vault(input), true), rw(cfg), ro(input.candidate, true), rw(programData(input.target)), ro(loader), ro(pending),
  ]);
}

export function createVaultTransaction(input: Vault & {
  creator: PublicKey; transactionIndex: bigint; recentBlockhash: string;
  instructions: TransactionInstruction[];
}) {
  return instructions.vaultTransactionCreate({
    multisigPda: input.multisigPda,
    vaultIndex: input.vaultIndex,
    transactionIndex: input.transactionIndex,
    creator: input.creator,
    ephemeralSigners: 0,
    programId: input.squadsProgram ?? PROGRAM_ID,
    transactionMessage: new TransactionMessage({
      payerKey: vault(input), instructions: input.instructions, recentBlockhash: input.recentBlockhash,
    }),
  });
}
