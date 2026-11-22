#!/usr/bin/env node
/**
 * canary: the client side of a gated upgrade.
 *
 *   canary config-address --gate G --target T
 *   canary window   --gate G --target T [--max-replay-txs 500]
 *   canary propose  --gate G --target T --multisig M --buffer B --elf PROGRAM.so --commit SHA [--vault-index 0]
 *                   [--spill S] [--replay-slots N] [--max-replay-txs 500] [--squads-program P]
 *   canary status   --gate G --target T [--nonce N]
 *   canary execute  --gate G --target T [--nonce N] [--priority-fee MICROLAMPORTS]
 *
 * RPC_URL (or --rpc) selects the cluster. propose and execute sign with the
 * keypair in CANARY_KEYPAIR (JSON array) or the file given by --keypair.
 * propose needs a multisig member with the Initiate permission. That member
 * cannot approve, so a leaked key can only open a proposal.
 */
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { ComputeBudgetProgram, Connection, Keypair, PublicKey, Transaction, TransactionInstruction } from "@solana/web3.js";
import { accounts, instructions, getProposalPda, getTransactionPda, PROGRAM_ID } from "@sqds/multisig";
import {
  configAddress, createVaultTransaction, executeGatedUpgrade, LOADER, proposalAddress, proposeBufferUpgrade, vaultAddress,
} from "./index.js";
import {
  checkBuffer, chooseWindow, decodeConfig, decodeProposal, executionState, latestNonce, sha256, trafficRate,
} from "./gate.js";

type Rpc = Pick<Connection,
  "getAccountInfo" | "getSignaturesForAddress" | "getSlot" | "getBlockTime" | "getLatestBlockhash"
  | "sendRawTransaction" | "confirmTransaction">;
type Target = { gate: PublicKey; target: PublicKey };

export async function readConfig(rpc: Rpc, { gate, target }: Target) {
  const address = configAddress({ gate, target });
  const info = await rpc.getAccountInfo(address, "finalized");
  if (!info || !info.owner.equals(gate)) throw new Error(`no gate config for ${target.toBase58()} at ${address.toBase58()}`);
  return { address, config: decodeConfig(info.data) };
}

export async function suggestWindow(rpc: Rpc, input: Target & { maxReplayTxs: number }) {
  const { config } = await readConfig(rpc, input);
  const [signatures, slot] = await Promise.all([
    rpc.getSignaturesForAddress(input.target, { limit: 1000 }, "finalized"), rpc.getSlot("finalized"),
  ]);
  const txPerSlot = trafficRate(signatures, slot);
  return {
    ...chooseWindow({ minReplaySlots: config.minReplaySlots, minReplayTxs: config.minReplayTxs,
                      maxReplayTxs: input.maxReplayTxs, txPerSlot }),
    txPerSlot, minReplayTxs: config.minReplayTxs, minReplaySlots: config.minReplaySlots,
  };
}

export async function nextTransactionIndex(rpc: Rpc, multisigPda: PublicKey) {
  const info = await rpc.getAccountInfo(multisigPda, "confirmed");
  if (!info) throw new Error(`no Squads multisig at ${multisigPda.toBase58()}`);
  const [multisig] = accounts.Multisig.fromAccountInfo(info);
  return BigInt(multisig.transactionIndex.toString()) + 1n;
}

export type ProposeOptions = Target & {
  multisigPda: PublicKey; vaultIndex: number; squadsProgram?: PublicKey; buffer: PublicKey; spill?: PublicKey;
  elf: Buffer; commit: string; replaySlots?: bigint; maxReplayTxs: number; creator: PublicKey;
  transactionIndex: bigint; recentBlockhash: string;
  sleep?: (ms: number) => Promise<void>; bufferAttempts?: number;
};

const pause = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

/**
 * The Solana CLI confirms the buffer write and the authority handoff at "confirmed".
 * Wait until the finalized bank holds the built program under the gate's authority.
 */
export async function waitForBuffer(rpc: Rpc, buffer: PublicKey, config: PublicKey, elf: Buffer,
                                    sleep = pause, attempts = 45) {
  let failure: unknown;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const info = await rpc.getAccountInfo(buffer, "finalized");
    try {
      if (!info || !info.owner.equals(LOADER)) throw new Error(`no loader-v3 buffer at ${buffer.toBase58()}`);
      checkBuffer(info.data, config, elf);
      return;
    } catch (error) {
      failure = error;
    }
    if (attempt < attempts) await sleep(2000);
  }
  throw failure;
}

/** The Squads vault transaction and proposal that ask the multisig to call `propose_buffer_upgrade`. */
export async function buildProposal(rpc: Rpc, options: ProposeOptions) {
  if (!/^[0-9a-f]{40}$/.test(options.commit)) throw new Error("--commit must be a full 40-character Git commit hash");
  const { address, config } = await readConfig(rpc, options);
  await waitForBuffer(rpc, options.buffer, address, options.elf, options.sleep, options.bufferAttempts);
  const window = options.replaySlots === undefined
    ? await suggestWindow(rpc, options)
    : { replaySlots: options.replaySlots, expectedTxs: null };
  if (window.replaySlots < config.minReplaySlots) {
    throw new Error(`the gate requires at least ${config.minReplaySlots} replay slots`);
  }
  const vault = vaultAddress(options);
  const gateInstruction = proposeBufferUpgrade({
    ...options, nonce: config.proposalNonce, spill: options.spill ?? vault, elfSha256: sha256(options.elf),
    gitCommit: Buffer.from(options.commit, "hex"), replaySlots: window.replaySlots,
  });
  const programId = options.squadsProgram ?? PROGRAM_ID;
  const wrapped = createVaultTransaction({ ...options, instructions: [gateInstruction] });
  const opened = instructions.proposalCreate({
    multisigPda: options.multisigPda, creator: options.creator, transactionIndex: options.transactionIndex, programId,
  });
  return {
    gateInstruction,
    instructions: [wrapped, opened],
    summary: {
      gateProposal: proposalAddress({ ...options, nonce: config.proposalNonce }).toBase58(),
      nonce: config.proposalNonce,
      squadsTransaction: getTransactionPda({ multisigPda: options.multisigPda, index: options.transactionIndex, programId })[0].toBase58(),
      squadsProposal: getProposalPda({ multisigPda: options.multisigPda, transactionIndex: options.transactionIndex, programId })[0].toBase58(),
      squadsTransactionIndex: options.transactionIndex,
      elfSha256: sha256(options.elf).toString("hex"),
      commit: options.commit,
      replaySlots: window.replaySlots,
      expectedReplayTxs: window.expectedTxs,
      replayMode: config.replayMode,
    },
  };
}

export async function proposalState(rpc: Rpc, input: Target & { nonce?: bigint }) {
  const { config } = await readConfig(rpc, input);
  const nonce = input.nonce ?? latestNonce(config);
  if (nonce === null) throw new Error("the current policy generation has no proposal");
  const address = proposalAddress({ ...input, nonce });
  const info = await rpc.getAccountInfo(address, "finalized");
  if (!info || !info.owner.equals(input.gate)) throw new Error(`no proposal ${nonce} at ${address.toBase58()}`);
  const proposal = decodeProposal(info.data);
  const slot = await rpc.getSlot("finalized");
  const now = BigInt((await rpc.getBlockTime(slot)) ?? Math.floor(Date.now() / 1000));
  const execution = executionState(proposal, config, now);
  return {
    config, proposal, execution,
    instruction: executeGatedUpgrade({ ...input, nonce, buffer: proposal.buffer, spill: proposal.spill }),
    summary: {
      proposal: address.toBase58(), nonce, status: proposal.status,
      votes: `${proposal.votes}/${config.threshold}`,
      voted: config.verifiers.filter((_, i) => proposal.voteBitmap & (1 << i)).map(key => key.toBase58()),
      replayMode: config.replayMode,
      elfSha256: proposal.elfSha256.toString("hex"), commit: proposal.gitCommit.toString("hex"),
      diffRoot: proposal.votes ? proposal.diffRoot.toString("hex") : null,
      window: { anchorSlot: proposal.anchorSlot, endSlot: proposal.anchorSlot + proposal.replaySlots, finalizedSlot: slot },
      expiresAt: new Date(Number(proposal.expiresAt) * 1000).toISOString(),
      executableNow: execution.ready, detail: execution.reason,
    },
  };
}

/** Upgrading verifies the whole new program, which can take hundreds of thousands of CU. */
export const EXECUTE_COMPUTE_UNITS = 1_400_000;

export async function send(rpc: Rpc, signer: Keypair, ixs: TransactionInstruction[], priorityFee = 0) {
  const { blockhash, lastValidBlockHeight } = await rpc.getLatestBlockhash("finalized");
  const price = priorityFee > 0 ? [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: priorityFee })] : [];
  const tx = new Transaction({ feePayer: signer.publicKey, blockhash, lastValidBlockHeight }).add(...price, ...ixs);
  tx.sign(signer);
  const signature = await rpc.sendRawTransaction(tx.serialize(), { preflightCommitment: "confirmed" });
  const result = await rpc.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, "confirmed");
  if (result.value.err) throw new Error(`transaction ${signature} failed: ${JSON.stringify(result.value.err)}`);
  return signature;
}

function loadKeypair(path?: string) {
  const raw = process.env.CANARY_KEYPAIR ?? (path ? readFileSync(path, "utf8") : undefined);
  if (!raw) throw new Error("set CANARY_KEYPAIR or pass --keypair");
  return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(raw)));
}

const json = (value: unknown) => JSON.stringify(value, (_, v) => (typeof v === "bigint" ? v.toString() : v), 2);

export async function main(argv: string[], rpcFor = (url: string) => new Connection(url, "finalized")) {
  const [command, ...rest] = argv;
  const { values: flags } = parseArgs({
    args: rest, strict: true,
    options: Object.fromEntries(["gate", "target", "multisig", "vault-index", "buffer", "spill", "elf", "commit",
      "replay-slots", "max-replay-txs", "squads-program", "nonce", "keypair", "rpc", "priority-fee"].map(name => [name, { type: "string" as const }])),
  });
  const need = (name: string) => {
    const value = flags[name];
    if (typeof value !== "string" || !value) throw new Error(`--${name} is required`);
    return value;
  };
  const target = { gate: new PublicKey(need("gate")), target: new PublicKey(need("target")) };
  if (command === "config-address") return { configAddress: configAddress(target).toBase58() };
  const url = (flags.rpc as string | undefined) ?? process.env.RPC_URL;
  if (!url) throw new Error("set RPC_URL or pass --rpc");
  const rpc = rpcFor(url);
  const maxReplayTxs = Number(flags["max-replay-txs"] ?? 500);
  const nonce = flags.nonce === undefined ? undefined : BigInt(flags.nonce as string);
  const priorityFee = Number(flags["priority-fee"] ?? 0);
  if (!Number.isSafeInteger(priorityFee) || priorityFee < 0) throw new Error("--priority-fee must be micro-lamports per CU");
  switch (command) {
    case "window":
      return suggestWindow(rpc, { ...target, maxReplayTxs });
    case "status":
      return (await proposalState(rpc, { ...target, nonce })).summary;
    case "execute": {
      const state = await proposalState(rpc, { ...target, nonce });
      if (!state.execution.ready) return { ...state.summary, executed: false };
      const budget = ComputeBudgetProgram.setComputeUnitLimit({ units: EXECUTE_COMPUTE_UNITS });
      const signature = await send(rpc, loadKeypair(flags.keypair as string | undefined), [budget, state.instruction],
                                   priorityFee);
      return { ...state.summary, executed: true, signature };
    }
    case "propose": {
      const signer = loadKeypair(flags.keypair as string | undefined);
      const multisigPda = new PublicKey(need("multisig"));
      const squadsProgram = flags["squads-program"] ? new PublicKey(flags["squads-program"] as string) : undefined;
      const built = await buildProposal(rpc, {
        ...target, multisigPda, vaultIndex: Number(flags["vault-index"] ?? 0), squadsProgram,
        buffer: new PublicKey(need("buffer")), spill: flags.spill ? new PublicKey(flags.spill as string) : undefined,
        elf: readFileSync(need("elf")), commit: need("commit"), maxReplayTxs, creator: signer.publicKey,
        replaySlots: flags["replay-slots"] === undefined ? undefined : BigInt(flags["replay-slots"] as string),
        transactionIndex: await nextTransactionIndex(rpc, multisigPda),
        recentBlockhash: (await rpc.getLatestBlockhash("finalized")).blockhash,
      });
      const signature = await send(rpc, signer, built.instructions, priorityFee);
      return { ...built.summary, signature };
    }
    default:
      throw new Error("usage: canary <config-address|window|propose|status|execute> --gate G --target T ...");
  }
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    result => console.log(json(result)),
    error => { console.error(`canary: ${error instanceof Error ? error.message : error}`); process.exit(1); },
  );
}

export { json };
