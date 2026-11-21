const { test } = require("node:test");
const assert = require("node:assert/strict");
const { Keypair, PublicKey } = require("@solana/web3.js");
const { getVaultPda, PROGRAM_ID } = require("@sqds/multisig");
const adapter = require("../dist/index.js");
const key = () => Keypair.generate().publicKey;

test("Squads signer, account order and instruction arguments", () => {
  const input = {
    gate: key(), target: key(), multisigPda: key(), vaultIndex: 0, nonce: 3n,
    buffer: key(), spill: key(), elfSha256: Buffer.alloc(32, 1),
    gitCommit: Buffer.alloc(20, 2), anchorSlot: 505n, replaySlots: 50n,
  };
  const [vault] = getVaultPda({ multisigPda: input.multisigPda, index: 0 });
  const ix = adapter.proposeBufferUpgrade(input);
  assert.throws(() => adapter.initializeGatekeeper({
    ...input, emergency: key(), policy: { replayMode: "strict" },
  }), /invalid replay mode/);
  assert.ok(ix.keys[0].pubkey.equals(vault) && ix.keys[0].isSigner && ix.keys[0].isWritable);
  assert.ok(ix.keys[1].pubkey.equals(vault) && ix.keys[1].isSigner);
  assert.ok(ix.keys[4].pubkey.equals(input.buffer));
  assert.equal(ix.data[92], 1);
  assert.equal(ix.data.readBigUInt64LE(93), 505n);
  assert.equal(ix.data.readBigUInt64LE(101), 50n);
  assert.ok(adapter.reclaimBuffer(input).keys[0].pubkey.equals(vault));
  assert.ok(adapter.executeGatedUpgrade(input).keys.every(k => !k.isSigner));
  const destination = key();
  const transfer = adapter.transferProgramAuthority({ ...input, candidate: destination });
  assert.ok(transfer.keys[2].isSigner && transfer.keys[2].pubkey.equals(destination));
  const wrap = adapter.createVaultTransaction({
    ...input, creator: key(), transactionIndex: 1n, instructions: [ix],
    recentBlockhash: PublicKey.default.toBase58(),
  });
  assert.ok(wrap.programId.equals(PROGRAM_ID));
});
