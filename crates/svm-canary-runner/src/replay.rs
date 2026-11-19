use anchor_lang::prelude::{Clock, Pubkey};
use anchor_lang::solana_program::bpf_loader_upgradeable as loader;
use anchor_lang::solana_program::sanitize::Sanitize;
use anyhow::{anyhow, ensure, Context, Result};
use litesvm::LiteSVM;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use solana_account::Account;
use solana_transaction::Transaction;
use std::collections::BTreeSet;

pub fn hash(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

#[derive(Serialize, Deserialize)]
pub struct Record {
    pub slot: u64,
    pub index: u32,
    pub transaction: Transaction,
}

/// A local legacy-transaction archive. Each listed clock covers one slot,
/// including slots without a target invocation. Other sysvars use LiteSVM genesis.
#[derive(Serialize, Deserialize)]
pub struct Archive {
    pub format_version: u8,
    pub transaction_manifest: [u8; 32],
    pub target: Pubkey,
    pub gate: Pubkey,
    pub buffer_key: Pubkey,
    pub nonce: u64,
    pub source_commit: String,
    pub expected_elf: [u8; 32],
    pub baseline_elf: [u8; 32],
    pub buffer: Account,
    pub anchor_slot: u64,
    pub replay_slots: u64,
    pub min_txs: u32,
    pub cu_limit_bps: u16,
    pub accounts: Vec<(Pubkey, Account)>,
    pub clocks: Vec<Clock>,
    pub records: Vec<Record>,
}

#[derive(Serialize, PartialEq, Eq, Debug)]
pub struct Evidence {
    pub elf_sha256: [u8; 32],
    pub diff_root: [u8; 32],
    pub replayed_tx_count: u32,
    pub max_cu_delta_bps: u16,
}

/// Commitment recorded at capture time, in canonical block order.
pub fn manifest<'a>(records: impl IntoIterator<Item = &'a Record>) -> Result<[u8; 32]> {
    let mut digest = Sha256::new();
    digest.update(b"CANARY_TX_LIST_V1");
    for r in records {
        digest.update(r.slot.to_le_bytes());
        digest.update(r.index.to_le_bytes());
        digest.update(
            r.transaction
                .signatures
                .first()
                .context("missing transaction signature")?
                .as_ref(),
        );
    }
    Ok(digest.finalize().into())
}

fn merkle(mut nodes: Vec<[u8; 32]>) -> [u8; 32] {
    while nodes.len() > 1 {
        nodes = nodes
            .chunks(2)
            .map(|pair| {
                let mut h = Sha256::new();
                h.update(b"CANARY_NODE_V1");
                h.update(pair[0]);
                h.update(*pair.last().unwrap());
                h.finalize().into()
            })
            .collect();
    }
    nodes[0]
}

pub fn verify(archive: &Archive, baseline: &[u8], rebuilt: &[u8]) -> Result<Evidence> {
    ensure!(
        archive.format_version == 1,
        "unsupported local archive version"
    );
    let expected_gate = Pubkey::find_program_address(
        &[b"gatekeeper_cfg", archive.target.as_ref()],
        &canary_upgrade_gate::ID,
    )
    .0;
    ensure!(
        archive.gate == expected_gate,
        "gate PDA differs from target"
    );
    ensure!(
        archive.buffer.owner == loader::ID && !archive.buffer.executable,
        "wrong buffer owner or flags"
    );
    let offset = loader::UpgradeableLoaderState::size_of_buffer_metadata();
    let data = &archive.buffer.data;
    let header = data.get(..offset).context("truncated loader metadata")?;
    let state: loader::UpgradeableLoaderState = bincode::deserialize(header)?;
    ensure!(
        matches!(state, loader::UpgradeableLoaderState::Buffer { authority_address: Some(k) } if k == archive.gate),
        "buffer authority mismatch"
    );
    ensure!(
        rebuilt.starts_with(b"\x7fELF") && baseline.starts_with(b"\x7fELF"),
        "expected ELF binaries"
    );
    let body = &data[offset..];
    // Padding follows the known rebuilt file length. Preserve meaningful trailing zero bytes.
    ensure!(
        body.starts_with(rebuilt) && body[rebuilt.len()..].iter().all(|b| *b == 0),
        "rebuilt ELF differs from locked buffer"
    );
    ensure!(
        hash(rebuilt) == archive.expected_elf && hash(baseline) == archive.baseline_elf,
        "ELF digest mismatch"
    );
    let count = u32::try_from(archive.records.len())?;
    ensure!(
        archive.min_txs > 0 && count >= archive.min_txs,
        "insufficient replay coverage"
    );
    ensure!(
        archive.replay_slots > 0
            && u64::try_from(archive.clocks.len())?.checked_sub(1) == Some(archive.replay_slots),
        "incomplete clock archive"
    );
    for (i, clock) in archive.clocks.iter().enumerate() {
        ensure!(
            Some(clock.slot) == archive.anchor_slot.checked_add(i as u64),
            "clock slot mismatch"
        );
    }
    let mut old = LiteSVM::new();
    old.set_sysvar(&archive.clocks[0]);
    let mut seeded = BTreeSet::new();
    for (key, account) in &archive.accounts {
        ensure!(
            seeded.insert(*key)
                && *key != archive.target
                && old.get_account(key).is_none()
                && !account.executable,
            "snapshot contains duplicate or reserved account"
        );
        old.set_account(*key, account.clone())?;
    }
    let mut new = old.clone();
    old.add_program(archive.target, baseline)?;
    new.add_program(archive.target, rebuilt)?;
    let mut records: Vec<_> = archive.records.iter().collect();
    records.sort_by_key(|r| (r.slot, r.index));
    ensure!(
        manifest(records.iter().copied())? == archive.transaction_manifest,
        "captured transaction manifest mismatch"
    );
    let mut positions = BTreeSet::new();
    let mut signatures = BTreeSet::new();
    let mut leaves = Vec::new();
    let mut peak = 0u16;
    for record in records {
        ensure!(
            positions.insert((record.slot, record.index)),
            "duplicate block position"
        );
        let i = record
            .slot
            .checked_sub(archive.anchor_slot)
            .context("transaction before anchor")?;
        let clock = archive
            .clocks
            .get(usize::try_from(i)?)
            .context("transaction after window")?;
        old.set_sysvar(clock);
        new.set_sysvar(clock);
        let tx = &record.transaction;
        tx.sanitize().context("invalid transaction structure")?;
        tx.verify().context("invalid transaction signature")?;
        ensure!(
            signatures.insert(*tx.signatures.first().context("missing signature")?),
            "duplicate transaction"
        );
        let msg = &tx.message;
        ensure!(
            !msg.instructions.is_empty()
                && msg
                    .instructions
                    .iter()
                    .all(|ix| msg.account_keys.get(ix.program_id_index as usize)
                        == Some(&archive.target)),
            "archive requires direct target invocations"
        );
        for key in &msg.account_keys {
            ensure!(
                *key == archive.target || seeded.contains(key),
                "missing account snapshot for {key}"
            );
        }
        let a = old.send_transaction(tx.clone()).map_err(|e| {
            anyhow!(
                "baseline failed at {}:{}: {:?}",
                record.slot,
                record.index,
                e
            )
        })?;
        let b = new.send_transaction(tx.clone()).map_err(|e| {
            anyhow!(
                "candidate failed at {}:{}: {:?}",
                record.slot,
                record.index,
                e
            )
        })?;
        ensure!(a.return_data == b.return_data, "return data changed");
        ensure!(a.compute_units_consumed > 0, "missing compute measurement");
        let growth = u128::from(
            b.compute_units_consumed
                .saturating_sub(a.compute_units_consumed),
        ) * 10_000;
        let bp = growth.div_ceil(u128::from(a.compute_units_consumed));
        ensure!(
            bp <= u128::from(archive.cu_limit_bps),
            "compute policy exceeded"
        );
        peak = peak.max(bp as u16);
        let writable: BTreeSet<_> = msg
            .account_keys
            .iter()
            .enumerate()
            .filter(|(i, _)| msg.is_maybe_writable(*i, None))
            .map(|(_, k)| *k)
            .collect();
        ensure!(!writable.is_empty(), "missing writable accounts");
        for key in writable {
            let before = old.get_account(&key);
            let after = new.get_account(&key);
            ensure!(
                before == after,
                "post-state differs for {key} at slot {}",
                record.slot
            );
            let mut leaf = Sha256::new();
            leaf.update(b"CANARY_STATE_V1");
            leaf.update(record.slot.to_le_bytes());
            leaf.update(record.index.to_le_bytes());
            leaf.update(tx.signatures[0].as_ref());
            leaf.update(key.as_ref());
            leaf.update(hash(&bincode::serialize(&after)?));
            leaves.push(leaf.finalize().into());
        }
    }
    Ok(Evidence {
        elf_sha256: hash(rebuilt),
        diff_root: merkle(leaves),
        replayed_tx_count: count,
        max_cu_delta_bps: peak,
    })
}
