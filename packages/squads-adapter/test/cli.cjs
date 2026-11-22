const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { mkdtempSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { Keypair, PublicKey, Transaction } = require("@solana/web3.js");
const { accounts, PROGRAM_ID } = require("@sqds/multisig");
const adapter = require("../dist/index.js");
const cli = require("../dist/cli.js");

const key = () => Keypair.generate().publicKey;
const accountTag = name => createHash("sha256").update("account:" + name).digest().subarray(0, 8);
const ixTag = name => createHash("sha256").update("global:" + name).digest().subarray(0, 8);
const GENERATION = 1n << 32n;

function configData({ target, verifiers, threshold = 2, mode = 1, minTxs = 50, minSlots = 150n, timelock = 3600n,
                      expiry = 86400n, nonce = GENERATION }) {
  const data = Buffer.alloc(371);
  accountTag("GatekeeperConfig").copy(data, 0);
  data[8] = 2;
  target.toBuffer().copy(data, 10);
  key().toBuffer().copy(data, 42);
  key().toBuffer().copy(data, 74);
  data[106] = mode;
  verifiers.forEach((verifier, i) => verifier.toBuffer().copy(data, 107 + 32 * i));
  data[331] = verifiers.length;
  data[332] = threshold;
  data.writeUInt16LE(1500, 333);
  data.writeUInt32LE(minTxs, 335);
  data.writeBigUInt64LE(minSlots, 339);
  data.writeBigInt64LE(timelock, 347);
  data.writeBigInt64LE(expiry, 355);
  data.writeBigUInt64LE(nonce, 363);
  return data;
}

function proposalData({ nonce, target, buffer, spill, status = 0, bitmap = 0, readyAt = 0n, expiresAt = 100_000n }) {
  const data = Buffer.alloc(248);
  accountTag("UpgradeProposal").copy(data, 0);
  data.writeBigUInt64LE(nonce, 9);
  target.toBuffer().copy(data, 17);
  buffer.toBuffer().copy(data, 57);
  spill.toBuffer().copy(data, 89);
  Buffer.alloc(32, 7).copy(data, 121);
  Buffer.alloc(20, 8).copy(data, 153);
  data.writeBigUInt64LE(500n, 173);
  data.writeBigUInt64LE(150n, 181);
  Buffer.alloc(32, 9).copy(data, 189);
  data[221] = bitmap;
  data[222] = [0, 1, 2].filter(i => bitmap & (1 << i)).length;
  data.writeBigInt64LE(expiresAt, 231);
  data.writeBigInt64LE(readyAt, 239);
  data[247] = status;
  return data;
}

function bufferData(authority, elf, padding = 16) {
  const data = Buffer.alloc(37 + elf.length + padding);
  data.writeUInt32LE(1, 0);
  data[4] = 1;
  authority.toBuffer().copy(data, 5);
  elf.copy(data, 37);
  return data;
}

function fakeRpc(entries, { signatures = [], slot = 10_000, blockTime = 1_000_000 } = {}) {
  const sent = [];
  const accountsByKey = new Map(entries.map(([address, owner, data]) => [address.toBase58(), { owner, data }]));
  return {
    sent,
    getAccountInfo: async address => accountsByKey.get(address.toBase58()) ?? null,
    getSignaturesForAddress: async () => signatures,
    getSlot: async () => slot,
    getBlockTime: async () => blockTime,
    getLatestBlockhash: async () => ({ blockhash: PublicKey.default.toBase58(), lastValidBlockHeight: 100 }),
    sendRawTransaction: async raw => { sent.push(Transaction.from(raw)); return "signature"; },
    confirmTransaction: async () => ({ value: { err: null } }),
  };
}

// 0.5 successful transactions per slot over the last 1,000 slots, plus failures that do not count.
const traffic = (rate, slot = 10_000) => [
  ...Array.from({ length: 1000 * rate }, (_, i) => ({ slot: slot - Math.floor(i / rate), err: null })),
  ...Array.from({ length: 50 }, () => ({ slot, err: { InstructionError: [0, "Custom"] } })),
];

test("the replay window fits the gate minimum and the plan cap", () => {
  // 500 successful transactions from slot 9,002 through the current slot 10,001.
  assert.equal(adapter.trafficRate(traffic(0.5), 10_001), 0.5);
  // 50 txs with 50% headroom needs 150 slots, the gate minimum. About 75 txs fit a 500 cap.
  assert.deepEqual(adapter.chooseWindow({ minReplaySlots: 150n, minReplayTxs: 50, maxReplayTxs: 500, txPerSlot: 0.5 }),
                   { replaySlots: 150n, expectedTxs: 75 });
  // A slow program needs a longer window than the gate minimum.
  assert.equal(adapter.chooseWindow({ minReplaySlots: 150n, minReplayTxs: 50, maxReplayTxs: 500, txPerSlot: 0.1 })
    .replaySlots, 750n);
  // A busy program cannot fit the gate minimum of 150 slots under the plan cap.
  assert.throws(() => adapter.chooseWindow({ minReplaySlots: 150n, minReplayTxs: 50, maxReplayTxs: 500, txPerSlot: 10 }),
                /exceed the plan's 500 transactions/);
  assert.throws(() => adapter.chooseWindow({ minReplaySlots: 150n, minReplayTxs: 50, maxReplayTxs: 500, txPerSlot: 0 }),
                /no recent successful transactions/);
});

test("propose wraps propose_buffer_upgrade for the multisig after checking the buffer", async () => {
  const gate = key(), target = key(), multisigPda = key(), buffer = key(), creator = key();
  const elf = Buffer.concat([Buffer.from("\x7fELF"), Buffer.alloc(60, 3)]);
  const config = adapter.configAddress({ gate, target });
  const nonce = GENERATION + 3n;
  const rpc = fakeRpc([
    [config, gate, configData({ target, verifiers: [key(), key(), key()], nonce })],
    [buffer, adapter.LOADER, bufferData(config, elf)],
  ], { signatures: traffic(0.5) });
  const options = { gate, target, multisigPda, vaultIndex: 0, buffer, elf, commit: "ab".repeat(20), maxReplayTxs: 500,
                    creator, transactionIndex: 7n, recentBlockhash: PublicKey.default.toBase58(),
                    sleep: async () => {}, bufferAttempts: 2 };
  const built = await cli.buildProposal(rpc, options);
  const data = built.gateInstruction.data;
  assert.deepEqual(data.subarray(0, 8), ixTag("propose_buffer_upgrade"));
  const [vault] = require("@sqds/multisig").getVaultPda({ multisigPda, index: 0 });
  assert.ok(new PublicKey(data.subarray(8, 40)).equals(vault), "the vault receives the buffer rent by default");
  assert.deepEqual(data.subarray(40, 72), createHash("sha256").update(elf).digest());
  assert.equal(data.subarray(72, 92).toString("hex"), "ab".repeat(20));
  assert.equal(data[92], 0, "the gate anchors the window at the slot the multisig executes");
  assert.equal(data.readBigUInt64LE(93), 150n);
  assert.ok(built.gateInstruction.keys[5].pubkey.equals(adapter.proposalAddress({ gate, target, nonce })));
  assert.deepEqual(built.instructions.map(ix => ix.programId.toBase58()), [PROGRAM_ID.toBase58(), PROGRAM_ID.toBase58()]);
  assert.equal(built.summary.squadsTransactionIndex, 7n);
  assert.equal(built.summary.expectedReplayTxs, 75);

  const refusals = [
    [{ commit: "abc" }, /full 40-character/],
    [{ replaySlots: 149n }, /at least 150 replay slots/],
    [{ elf: Buffer.concat([elf.subarray(0, 63), Buffer.from([4])]) }, /does not hold the built program/],
  ];
  for (const [change, reason] of refusals) {
    await assert.rejects(cli.buildProposal(rpc, { ...options, ...change }), reason);
  }
  const foreign = fakeRpc([
    [config, gate, configData({ target, verifiers: [key(), key()], nonce })],
    [buffer, adapter.LOADER, bufferData(key(), elf)],
  ], { signatures: traffic(0.5) });
  await assert.rejects(cli.buildProposal(foreign, options), /buffer authority must be the gate config/);
});

test("propose waits until the finalized bank shows the authority handoff", async () => {
  const gate = key(), target = key(), buffer = key();
  const elf = Buffer.from("ELF candidate");
  const config = adapter.configAddress({ gate, target });
  // The Solana CLI returns at "confirmed": the first finalized reads still show the uploader.
  const states = [bufferData(key(), elf), bufferData(key(), elf), bufferData(config, elf)];
  const waits = [];
  const rpc = { getAccountInfo: async () => ({ owner: adapter.LOADER, data: states.shift() ?? bufferData(config, elf) }) };
  await cli.waitForBuffer(rpc, buffer, config, elf, async ms => { waits.push(ms); }, 5);
  assert.deepEqual(waits, [2000, 2000]);
  const stale = { getAccountInfo: async () => ({ owner: adapter.LOADER, data: bufferData(key(), elf) }) };
  await assert.rejects(cli.waitForBuffer(stale, buffer, config, elf, async () => {}, 3), /buffer authority must be the gate/);
});

test("the next Squads transaction index comes from the multisig account", async () => {
  const multisigPda = key();
  const [data] = accounts.Multisig.fromArgs({
    createKey: key(), configAuthority: PublicKey.default, threshold: 2, timeLock: 0, transactionIndex: 6,
    staleTransactionIndex: 0, rentCollector: null, bump: 255, members: [{ key: key(), permissions: { mask: 7 } }],
  }).serialize();
  assert.equal(await cli.nextTransactionIndex(fakeRpc([[multisigPda, PROGRAM_ID, data]]), multisigPda), 7n);
});

test("status reports votes and execute waits for quorum, timelock and expiry", async () => {
  const gate = key(), target = key(), buffer = key(), spill = key();
  const verifiers = [key(), key(), key()];
  const nonce = GENERATION;
  const accountsFor = proposal => [
    [adapter.configAddress({ gate, target }), gate, configData({ target, verifiers, nonce: GENERATION + 1n })],
    [adapter.proposalAddress({ gate, target, nonce }), gate, proposalData({ nonce, target, buffer, spill, ...proposal })],
  ];
  const ready = { status: 1, bitmap: 0b101, readyAt: 1_000n, expiresAt: 100_000n };
  const args = ["--gate", gate.toBase58(), "--target", target.toBase58(), "--rpc", "http://localhost"];

  const status = await cli.main(["status", ...args], () => fakeRpc(accountsFor(ready), { blockTime: 4_599 }));
  assert.equal(status.status, "Ready");
  assert.equal(status.votes, "2/2");
  assert.deepEqual(status.voted, [verifiers[0].toBase58(), verifiers[2].toBase58()]);
  assert.equal(status.executableNow, false);
  assert.match(status.detail, /timelock ends in 1 seconds/);

  const dir = mkdtempSync(join(tmpdir(), "canary-"));
  writeFileSync(join(dir, "payer.json"), JSON.stringify(Array.from(Keypair.generate().secretKey)));
  const waiting = fakeRpc(accountsFor(ready), { blockTime: 4_599 });
  const early = await cli.main(["execute", ...args, "--keypair", join(dir, "payer.json")], () => waiting);
  assert.equal(early.executed, false);
  assert.equal(waiting.sent.length, 0);

  const due = fakeRpc(accountsFor(ready), { blockTime: 4_600 });
  const executed = await cli.main(["execute", ...args, "--keypair", join(dir, "payer.json")], () => due);
  assert.equal(executed.executed, true);
  const [tx] = due.sent;
  const [budget, ix] = tx.instructions;
  // Upgrading verifies the whole new program, far beyond the default 200,000 CU.
  assert.equal(budget.programId.toBase58(), "ComputeBudget111111111111111111111111111111");
  assert.deepEqual([budget.data[0], budget.data.readUInt32LE(1)], [2, 1_400_000]);
  assert.ok(ix.programId.equals(gate));
  assert.deepEqual(ix.data.subarray(0, 8), ixTag("execute_gated_upgrade"));
  assert.ok(ix.keys[1].pubkey.equals(adapter.proposalAddress({ gate, target, nonce })));
  assert.ok(ix.keys[4].pubkey.equals(buffer) && ix.keys[5].pubkey.equals(spill));

  const priced = fakeRpc(accountsFor(ready), { blockTime: 4_600 });
  await cli.main(["execute", ...args, "--keypair", join(dir, "payer.json"), "--priority-fee", "5000"], () => priced);
  const price = priced.sent[0].instructions[0];
  assert.deepEqual([price.data[0], price.data.readBigUInt64LE(1)], [3, 5000n]);
  await assert.rejects(cli.main(["execute", ...args, "--priority-fee=-1"], () => priced), /micro-lamports/);

  for (const [proposal, blockTime, reason] of [
    [{ status: 0 }, 4_600, /is Pending/], [ready, 100_001, /expired/], [{ status: 2 }, 4_600, /is Executed/],
  ]) {
    const result = await cli.main(["status", ...args], () => fakeRpc(accountsFor(proposal), { blockTime }));
    assert.match(result.detail, reason);
  }
  const fresh = [[adapter.configAddress({ gate, target }), gate, configData({ target, verifiers, nonce: GENERATION })]];
  await assert.rejects(cli.main(["status", ...args], () => fakeRpc(fresh)), /has no proposal/);
});
