const { test } = require("node:test");
const assert = require("node:assert/strict");
const { Keypair, PublicKey, SYSVAR_CLOCK_PUBKEY, SYSVAR_RENT_PUBKEY, SystemProgram } = require("@solana/web3.js");
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

const { createHash } = require("node:crypto");
const loader = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");
const pda = (program, ...seeds) => PublicKey.findProgramAddressSync(seeds, program)[0];
const tag = name => createHash("sha256").update("global:" + name).digest().subarray(0, 8);
// [pubkey, isSigner, isWritable] in the order of the gate's #[derive(Accounts)] structs.
const accounts = ix => ix.keys.map(k => [k.pubkey.toBase58(), k.isSigner, k.isWritable]);
const expect = (...metas) => metas.map(([pubkey, signer, writable]) => [pubkey.toBase58(), signer, writable]);

test("governance instructions follow the gate's account order", () => {
  const input = {
    gate: key(), target: key(), multisigPda: key(), vaultIndex: 1, nonce: 7n, buffer: key(), spill: key(),
  };
  const [vault] = getVaultPda({ multisigPda: input.multisigPda, index: 1 });
  const cfg = pda(input.gate, Buffer.from("gatekeeper_cfg"), input.target.toBuffer());
  const nonce = Buffer.alloc(8);
  nonce.writeBigUInt64LE(7n);
  const proposal = pda(input.gate, Buffer.from("proposal"), input.target.toBuffer(), nonce);
  const reservation = pda(input.gate, Buffer.from("buffer"), cfg.toBuffer(), input.buffer.toBuffer());
  const policyPda = pda(input.gate, Buffer.from("policy"), cfg.toBuffer());
  const authorityPda = pda(input.gate, Buffer.from("authority"), cfg.toBuffer());
  const programData = pda(loader, input.target.toBuffer());
  const check = (ix, name, metas) => {
    assert.ok(ix.programId.equals(input.gate));
    assert.deepEqual(ix.data.subarray(0, 8), tag(name), name);
    assert.deepEqual(accounts(ix), expect(...metas), name);
  };

  check(adapter.cancelProposal(input), "cancel_proposal",
    [[vault, true, false], [cfg, false, false], [proposal, false, true]]);
  check(adapter.closeProposal(input), "close_proposal",
    [[vault, true, false], [cfg, false, false], [proposal, false, true], [reservation, false, true], [input.spill, false, true]]);
  check(adapter.reclaimOrphanBuffer(input), "reclaim_orphan_buffer",
    [[vault, true, false], [cfg, false, false], [input.buffer, false, true], [reservation, false, false],
     [input.spill, false, true], [loader, false, false]]);

  const verifiers = [key(), key(), key()];
  const policy = {
    verifiers, threshold: 2, maxCuDeltaBps: 500, minReplayTxs: 50, minReplaySlots: 150n,
    timelockSeconds: 3600n, proposalExpirySeconds: 86400n, replayMode: "NoRevertAndCuBound",
  };
  const queue = adapter.proposeConfigUpdate({ ...input, policy });
  check(queue, "propose_config_update", [
    [vault, true, true], [vault, true, false], [cfg, false, false], [policyPda, false, true],
    [SystemProgram.programId, false, false],
  ]);
  // Borsh Policy: mode, 7 verifier slots, count, threshold, bps, min txs, min slots, timelock, expiry.
  assert.equal(queue.data.length, 8 + 1 + 7 * 32 + 32);
  assert.equal(queue.data[8], 1);
  assert.ok(new PublicKey(queue.data.subarray(9, 41)).equals(verifiers[0]));
  assert.deepEqual(queue.data.subarray(9 + 3 * 32, 9 + 7 * 32), Buffer.alloc(4 * 32));
  const fields = queue.data.subarray(9 + 7 * 32);
  assert.deepEqual([fields[0], fields[1], fields.readUInt16LE(2), fields.readUInt32LE(4)], [3, 2, 500, 50]);
  assert.deepEqual([fields.readBigUInt64LE(8), fields.readBigInt64LE(16), fields.readBigInt64LE(24)], [150n, 3600n, 86400n]);
  // The initializer shares the encoding and appends the emergency authority.
  const emergency = key();
  const init = adapter.initializeGatekeeper({ ...input, emergency, policy });
  assert.deepEqual(init.data.subarray(8, -32), queue.data.subarray(8));
  assert.ok(new PublicKey(init.data.subarray(-32)).equals(emergency));
  assert.throws(() => adapter.proposeConfigUpdate({ ...input, policy: { ...policy, threshold: 1 } }), /quorum/);
  assert.throws(() => adapter.proposeConfigUpdate({ ...input, policy: { ...policy, replayMode: "Loose" } }), /replay mode/);

  const payer = key();
  for (const [build, name] of [[adapter.executeConfigUpdate, "execute_config_update"],
                               [adapter.cancelConfigUpdate, "cancel_config_update"]]) {
    check(build(input), name, [[vault, true, false], [cfg, false, true], [policyPda, false, true], [vault, false, true]]);
    check(build({ ...input, refund: payer }), name,
      [[vault, true, false], [cfg, false, true], [policyPda, false, true], [payer, false, true]]);
  }

  check(adapter.emergencyOverrideUpgrade({ ...input, emergency }), "emergency_override_upgrade", [
    [emergency, true, false], [cfg, false, false], [input.target, false, true], [programData, false, true],
    [input.buffer, false, true], [input.spill, false, true], [SYSVAR_RENT_PUBKEY, false, false],
    [SYSVAR_CLOCK_PUBKEY, false, false], [loader, false, false],
  ]);

  const candidate = key();
  const rotate = adapter.proposeAuthorityChange({ ...input, role: "Emergency", candidate });
  check(rotate, "propose_authority_change", [
    [vault, true, true], [vault, true, false], [cfg, false, false], [authorityPda, false, true],
    [SystemProgram.programId, false, false],
  ]);
  assert.equal(rotate.data[8], 1);
  assert.ok(new PublicKey(rotate.data.subarray(9)).equals(candidate));
  assert.equal(adapter.proposeProgramAuthorityTransfer({ ...input, candidate }).data[8], 2);
  assert.throws(() => adapter.proposeAuthorityChange({ ...input, role: "Owner", candidate }), /role/);
  check(adapter.executeAuthorityChange({ ...input, candidate }), "execute_authority_change", [
    [vault, true, false], [cfg, false, true], [authorityPda, false, true], [vault, false, true], [candidate, true, false],
  ]);
  check(adapter.cancelAuthorityChange(input), "cancel_authority_change",
    [[vault, true, false], [cfg, false, true], [authorityPda, false, true], [vault, false, true]]);
});
