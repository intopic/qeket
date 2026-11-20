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
const proposal = ({gate, target, nonce}: Upgrade) => derive(gate, Buffer.from("proposal"), target.toBuffer(), u64(nonce));

function ix(gate: PublicKey, name: string, keys: AccountMeta[], args = Buffer.alloc(0)) {
  const tag = createHash("sha256").update("global:" + name).digest().subarray(0, 8);
  return new TransactionInstruction({ programId: gate, keys, data: Buffer.concat([tag, args]) });
}

function vault(input: Vault) {
  return getVaultPda({ multisigPda: input.multisigPda, index: input.vaultIndex, programId: input.squadsProgram ?? PROGRAM_ID })[0];
}

export function initializeGatekeeper(input: Context & Vault & { emergency: PublicKey; policy: Policy }) {
  const { gate, target, emergency, policy } = input;
  const governance = vault(input);
  if (policy.verifiers.length > 7 || policy.threshold < 2 ||
      policy.threshold > policy.verifiers.length || new Set(policy.verifiers.map(k => k.toBase58())).size !== policy.verifiers.length) {
    throw new Error("invalid verifier quorum");
  }
  if (governance.equals(emergency) || emergency.equals(PublicKey.default)) throw new Error("invalid emergency authority");
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
  const data = Buffer.concat([
    Buffer.of(policy.replayMode === "StrictEquality" ? 0 : 1), verifierKeys, fields, emergency.toBuffer(),
  ]);
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
  const cfg = config(input);
  const reservation = derive(gate, Buffer.from("buffer"), cfg.toBuffer(), buffer.toBuffer());
  const anchor = anchorSlot === undefined ? Buffer.of(0) : Buffer.concat([Buffer.of(1), u64(anchorSlot)]);
  return ix(gate, "propose_buffer_upgrade", [
    rw(governance, true), ro(governance, true), rw(cfg), ro(programData(target)),
    ro(buffer), rw(proposal(input)), rw(reservation), ro(SystemProgram.programId),
  ], Buffer.concat([spill.toBuffer(), elfSha256, gitCommit, anchor, u64(replaySlots)]));
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

export function proposeProgramAuthorityTransfer(input: Context & Vault & {candidate: PublicKey}) {
  const governance = vault(input);
  const cfg = config(input);
  return ix(input.gate, "propose_authority_change", [
    rw(governance, true), ro(governance, true), ro(cfg),
    rw(derive(input.gate, Buffer.from("authority"), cfg.toBuffer())), ro(SystemProgram.programId),
  ], Buffer.concat([Buffer.of(2), input.candidate.toBuffer()]));
}

export function transferProgramAuthority(input: Context & Vault & {candidate: PublicKey; emergency?: boolean}) {
  const cfg = config(input);
  const pending = input.emergency ? input.gate : derive(input.gate, Buffer.from("authority"), cfg.toBuffer());
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
