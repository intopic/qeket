use anchor_lang::prelude::{Clock, Pubkey};
use anchor_lang::solana_program::bpf_loader_upgradeable as loader;
use anchor_lang::solana_program::{
    address_lookup_table::state::AddressLookupTable, slot_hashes::SlotHashes,
};
use anyhow::{anyhow, ensure, Context, Result};
use canary_upgrade_gate::state::ReplayMode;
use litesvm::types::TransactionMetadata;
use litesvm::LiteSVM;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use solana_account::Account;
use solana_transaction::versioned::VersionedTransaction;
use solana_transaction::Transaction;
use std::collections::BTreeSet;

pub fn hash(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "ReplayMode")]
enum Mode {
    StrictEquality,
    NoRevertAndCuBound,
}

mod wire_transaction {
    use super::*;
    use bincode::Options;
    use serde::{de::Error, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(
        tx: &VersionedTransaction,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        let bytes = bincode::serialize(tx).map_err(serde::ser::Error::custom)?;
        serializer.serialize_str(&hex::encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<VersionedTransaction, D::Error> {
        let value = serde_json::Value::deserialize(d)?;
        if let Some(wire) = value.as_str() {
            if wire.len() > 2 * 1232 {
                return Err(D::Error::custom("oversized Solana transaction"));
            }
            let bytes = hex::decode(wire).map_err(D::Error::custom)?;
            return bincode::options()
                .with_fixint_encoding()
                .with_limit(1232)
                .reject_trailing_bytes()
                .deserialize(&bytes)
                .map_err(D::Error::custom);
        }
        // Before format 2 the archive stored a JSON legacy Transaction.
        serde_json::from_value::<Transaction>(value)
            .map(Into::into)
            .map_err(D::Error::custom)
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Record {
    pub slot: u64,
    pub index: u32,
    #[serde(with = "wire_transaction")]
    pub transaction: VersionedTransaction,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub baseline: Option<BaselineObservation>,
}

/// Finalized `meta` of the baseline execution. Replay must reproduce it exactly.
#[derive(Clone, Serialize, Deserialize)]
pub struct BaselineObservation {
    pub keys: Vec<Pubkey>,
    pub pre_balances: Vec<u64>,
    pub post_balances: Vec<u64>,
    pub compute_units: u64,
    pub fee: u64,
}

const LAMPORTS_PER_SIGNATURE: u64 = 5_000;
const MAX_COMPUTE_UNIT_LIMIT: u32 = 1_400_000;

/// Signature fee and compute-unit price fee charged by the bank.
/// LiteSVM 0.7 charges only the first part.
fn fee_parts(tx: &VersionedTransaction) -> Result<(u64, u64)> {
    use solana_sdk_ids::{compute_budget, ed25519_program, secp256k1_program, secp256r1_program};
    let keys = tx.message.static_account_keys();
    let mut signatures = u64::from(tx.message.header().num_required_signatures);
    let (mut limit, mut price) = (None, None);
    for ix in tx.message.instructions() {
        let program = keys
            .get(usize::from(ix.program_id_index))
            .context("invalid program index")?;
        if *program == compute_budget::ID {
            let (tag, value) = ix
                .data
                .split_first()
                .context("empty compute budget instruction")?;
            match (tag, value.len()) {
                (2, 4) => ensure!(
                    limit
                        .replace(u32::from_le_bytes(value.try_into()?))
                        .is_none(),
                    "duplicate compute unit limit"
                ),
                (3, 8) => ensure!(
                    price
                        .replace(u64::from_le_bytes(value.try_into()?))
                        .is_none(),
                    "duplicate compute unit price"
                ),
                (1 | 4, 4) => {}
                _ => anyhow::bail!("unsupported compute budget instruction"),
            }
        } else if [
            ed25519_program::ID,
            secp256k1_program::ID,
            secp256r1_program::ID,
        ]
        .contains(program)
        {
            signatures += u64::from(ix.data.first().copied().unwrap_or(0));
        }
    }
    let priority = match price.unwrap_or(0) {
        0 => 0,
        // The default limit depends on the bank's builtin classification. Refuse
        // rather than estimate it.
        price => {
            let limit = limit
                .context("compute unit price without an explicit compute unit limit")?
                .min(MAX_COMPUTE_UNIT_LIMIT);
            u64::try_from((u128::from(price) * u128::from(limit)).div_ceil(1_000_000))?
        }
    };
    Ok((
        signatures
            .checked_mul(LAMPORTS_PER_SIGNATURE)
            .context("fee overflow")?,
        priority,
    ))
}

fn debit(vm: &mut LiteSVM, key: &Pubkey, lamports: u64) -> Result<()> {
    let mut account = vm.get_account(key).context("missing fee payer")?;
    account.lamports = account
        .lamports
        .checked_sub(lamports)
        .context("fee payer cannot cover the observed priority fee")?;
    vm.set_account(*key, account)?;
    Ok(())
}

impl BaselineObservation {
    /// LiteSVM installs the target with its own loader and balance, so that
    /// program account is the one entry the chain balance cannot describe.
    fn check_balances(
        &self,
        baseline: &LiteSVM,
        keys: &[Pubkey],
        balances: &[u64],
        emulated: &Pubkey,
    ) -> Result<()> {
        ensure!(
            self.keys == keys && balances.len() == keys.len(),
            "observed account list differs"
        );
        for (key, expected) in keys.iter().zip(balances) {
            if key == emulated {
                continue;
            }
            let actual = baseline
                .get_account(key)
                .map_or(0, |account| account.lamports);
            ensure!(
                actual == *expected,
                "observed balance differs for {key}: {actual} != {expected}"
            );
        }
        Ok(())
    }
}

/// A signed legacy/v0 transaction archive. Each listed clock covers one slot,
/// including slots without a target invocation. Other sysvars use LiteSVM genesis.
#[derive(Clone, Serialize, Deserialize)]
pub struct Archive {
    pub format_version: u8,
    #[serde(default, with = "Mode")]
    pub replay_mode: ReplayMode,
    #[serde(default)]
    pub absent_accounts: Vec<Pubkey>,
    #[serde(default)]
    pub slot_hashes: Vec<Vec<(u64, anchor_lang::solana_program::hash::Hash)>>,
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
    #[serde(with = "Mode")]
    pub replay_mode: ReplayMode,
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
        archive.format_version == 2
            || (archive.format_version == 1 && archive.replay_mode == ReplayMode::StrictEquality),
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
    ensure!(
        archive.slot_hashes.is_empty() || archive.slot_hashes.len() == archive.clocks.len(),
        "incomplete slot hashes"
    );
    // Signatures remain verified. Historical blockhashes are outside the local queue.
    let mut old = LiteSVM::new().with_blockhash_check(false);
    old.set_sysvar(&archive.clocks[0]);
    let mut seeded = BTreeSet::new();
    for key in &archive.absent_accounts {
        ensure!(
            *key != archive.target && old.get_account(key).is_none() && seeded.insert(*key),
            "invalid absent account"
        );
    }
    // Loader v3 needs the captured ProgramData in place when it loads the executable.
    for executable in [false, true] {
        for (key, account) in archive
            .accounts
            .iter()
            .filter(|(_, a)| a.executable == executable)
        {
            ensure!(
                seeded.insert(*key) && *key != archive.target,
                "duplicate or reserved snapshot account"
            );
            if let Some(existing) = old.get_account(key) {
                ensure!(
                    existing.owner != solana_sdk_ids::native_loader::ID
                        && existing.owner != solana_sdk_ids::sysvar::ID
                        && existing.executable == executable
                        && (executable
                            || (existing.owner == loader::ID && account.owner == loader::ID)),
                    "reserved snapshot account"
                );
            }
            if executable {
                ensure!(
                    account.owner == loader::ID
                        || account.owner == solana_sdk_ids::bpf_loader::ID
                        || account.owner == solana_sdk_ids::bpf_loader_deprecated::ID,
                    "unsupported program owner"
                );
                if account.owner == loader::ID {
                    let pd = loader::get_program_data_address(key);
                    let state =
                        bincode::deserialize::<loader::UpgradeableLoaderState>(&account.data)?;
                    ensure!(
                        matches!(state, loader::UpgradeableLoaderState::Program {programdata_address} if programdata_address == pd)
                            && seeded.contains(&pd)
                            && old.get_account(&pd).is_some_and(|a| a.owner == loader::ID
                                && !a.executable
                                && matches!(
                                    bincode::deserialize::<loader::UpgradeableLoaderState>(&a.data),
                                    Ok(loader::UpgradeableLoaderState::ProgramData { .. })
                                )),
                        "missing or invalid captured ProgramData"
                    );
                }
            }
            old.set_account(*key, account.clone())?;
        }
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
        if let Some(hashes) = archive.slot_hashes.get(i as usize) {
            let hashes = SlotHashes::new(hashes);
            old.set_sysvar(&hashes);
            new.set_sysvar(&hashes);
        }
        ensure!(
            signatures.insert(*tx.signatures.first().context("missing signature")?),
            "duplicate transaction"
        );
        let msg = &tx.message;
        let keys = resolve_keys(&old, tx, clock.slot, !archive.slot_hashes.is_empty())?;
        if !msg.address_table_lookups().unwrap_or_default().is_empty() {
            ensure!(
                keys == resolve_keys(&new, tx, clock.slot, !archive.slot_hashes.is_empty())?,
                "lookup resolution diverged"
            );
        }
        for key in &keys {
            ensure!(
                *key == archive.target
                    || seeded.contains(key)
                    || old
                        .get_account(key)
                        .is_some_and(|a| a.owner == solana_sdk_ids::native_loader::ID
                            || a.owner == solana_sdk_ids::sysvar::ID),
                "missing account snapshot for {key}"
            );
        }
        if let Some(observed) = &record.baseline {
            observed
                .check_balances(&old, &keys, &observed.pre_balances, &archive.target)
                .with_context(|| {
                    format!("baseline pre-state at {}:{}", record.slot, record.index)
                })?;
            let (signature_fee, priority_fee) = fee_parts(tx)
                .with_context(|| format!("fee at {}:{}", record.slot, record.index))?;
            ensure!(
                signature_fee.checked_add(priority_fee) == Some(observed.fee),
                "observed fee differs at {}:{}: {} != {signature_fee} + {priority_fee}",
                record.slot,
                record.index,
                observed.fee
            );
            // The bank debits the full fee before execution. Apply the part
            // LiteSVM omits so programs and later transactions see the same balance.
            if priority_fee > 0 {
                debit(&mut old, &keys[0], priority_fee)?;
                debit(&mut new, &keys[0], priority_fee)?;
            }
        }
        let a = old.send_transaction(tx.clone()).map_err(|e| {
            anyhow!(
                "baseline failed at {}:{}: {:?}",
                record.slot,
                record.index,
                e
            )
        })?;
        if let Some(observed) = &record.baseline {
            observed
                .check_balances(&old, &keys, &observed.post_balances, &archive.target)
                .with_context(|| {
                    format!("baseline post-state at {}:{}", record.slot, record.index)
                })?;
            ensure!(
                observed.compute_units == a.compute_units_consumed,
                "observed baseline compute differs at {}:{}",
                record.slot,
                record.index
            );
        }
        let b = new.send_transaction(tx.clone()).map_err(|e| {
            anyhow!(
                "candidate failed at {}:{}: {:?}",
                record.slot,
                record.index,
                e
            )
        })?;
        ensure!(
            invoked(tx, &keys, archive.target, &a) && invoked(tx, &keys, archive.target, &b),
            "target was not invoked"
        );
        let strict = archive.replay_mode == ReplayMode::StrictEquality;
        ensure!(
            !strict || a.return_data == b.return_data,
            "return data changed"
        );
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
        let writable: BTreeSet<_> = keys
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
                !strict || before == after,
                "post-state differs for {key} at slot {}, index {}",
                record.slot,
                record.index
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
        replay_mode: archive.replay_mode,
        elf_sha256: hash(rebuilt),
        diff_root: merkle(leaves),
        replayed_tx_count: count,
        max_cu_delta_bps: peak,
    })
}

fn resolve_keys(
    vm: &LiteSVM,
    tx: &VersionedTransaction,
    slot: u64,
    historical_hashes: bool,
) -> Result<Vec<Pubkey>> {
    let mut keys = tx.message.static_account_keys().to_vec();
    let mut readonly = Vec::new();
    for lookup in tx.message.address_table_lookups().unwrap_or_default() {
        let account = vm
            .get_account(&lookup.account_key)
            .context("missing lookup table")?;
        ensure!(
            account.owner == solana_sdk_ids::address_lookup_table::ID,
            "wrong lookup table owner"
        );
        let table = AddressLookupTable::deserialize(&account.data)?;
        ensure!(
            usize::from(table.meta.last_extended_slot_start_index) <= table.addresses.len(),
            "invalid lookup table metadata"
        );
        ensure!(
            historical_hashes || table.meta.deactivation_slot == u64::MAX,
            "deactivating table requires archived slot hashes"
        );
        let hashes = vm.get_sysvar::<SlotHashes>();
        keys.extend(table.lookup(slot, &lookup.writable_indexes, &hashes)?);
        readonly.extend(table.lookup(slot, &lookup.readonly_indexes, &hashes)?);
    }
    keys.extend(readonly);
    Ok(keys)
}

fn invoked(
    tx: &VersionedTransaction,
    keys: &[Pubkey],
    target: Pubkey,
    meta: &TransactionMetadata,
) -> bool {
    tx.message
        .instructions()
        .iter()
        .chain(
            meta.inner_instructions
                .iter()
                .flatten()
                .map(|inner| &inner.instruction),
        )
        .any(|ix| keys.get(usize::from(ix.program_id_index)) == Some(&target))
}

#[cfg(test)]
mod tests {
    use super::*;
    use solana_keypair::Keypair;
    use solana_signer::Signer;

    #[test]
    fn format_one_transaction_retains_its_signed_bytes() {
        let payer = Keypair::new();
        let legacy = Transaction::new_signed_with_payer(
            &[],
            Some(&payer.pubkey()),
            &[&payer],
            Default::default(),
        );
        let json = serde_json::json!({"slot": 1, "index": 2, "transaction": legacy});
        let record: Record = serde_json::from_value(json).unwrap();
        assert_eq!(
            bincode::serialize(&record.transaction).unwrap(),
            bincode::serialize(&legacy).unwrap()
        );
    }

    #[test]
    fn baseline_observation_rejects_mismatched_balances_and_loaded_keys() {
        let mut vm = LiteSVM::new();
        let payer = Pubkey::new_unique();
        let nonexistent = Pubkey::new_unique();
        vm.airdrop(&payer, 100_000).unwrap();
        let observation = BaselineObservation {
            keys: vec![payer, nonexistent],
            pre_balances: vec![100_000, 0],
            post_balances: vec![95_000, 0],
            compute_units: 42,
            fee: 5_000,
        };
        let reversed = [nonexistent, payer];
        assert!(observation
            .check_balances(
                &vm,
                &observation.keys,
                &observation.pre_balances,
                &Pubkey::default()
            )
            .is_ok());
        assert!(observation
            .check_balances(
                &vm,
                &observation.keys,
                &observation.post_balances,
                &Pubkey::default()
            )
            .is_err());
        assert!(observation
            .check_balances(
                &vm,
                &reversed,
                &observation.pre_balances,
                &Pubkey::default()
            )
            .is_err());
        // The target program account is emulated and has no chain balance.
        assert!(observation
            .check_balances(&vm, &observation.keys, &observation.post_balances, &payer)
            .is_ok());
    }

    #[test]
    fn priority_fee_is_recomputed_and_applied_before_execution() {
        use solana_compute_budget_interface::ComputeBudgetInstruction as Budget;
        let payer = Keypair::new();
        let recipient = Pubkey::new_unique();
        let transfer = anchor_lang::solana_program::system_instruction::transfer(
            &payer.pubkey(),
            &recipient,
            1_000,
        );
        let signed = |instructions: &[anchor_lang::solana_program::instruction::Instruction]| {
            VersionedTransaction::from(Transaction::new_signed_with_payer(
                instructions,
                Some(&payer.pubkey()),
                &[&payer],
                Default::default(),
            ))
        };
        let priced = signed(&[
            Budget::set_compute_unit_limit(200_000),
            Budget::set_compute_unit_price(1_001),
            transfer.clone(),
        ]);
        assert_eq!(fee_parts(&signed(&[transfer.clone()])).unwrap(), (5_000, 0));
        // ceil(1_001 * 200_000 / 1_000_000) lamports.
        assert_eq!(fee_parts(&priced).unwrap(), (5_000, 201));
        assert!(fee_parts(&signed(&[
            Budget::set_compute_unit_price(1),
            transfer.clone()
        ]))
        .is_err());
        assert!(fee_parts(&signed(&[
            Budget::set_compute_unit_limit(1),
            Budget::set_compute_unit_limit(2),
            transfer.clone(),
        ]))
        .is_err());
        let precompile = solana_ed25519_program::new_ed25519_instruction_with_signature(
            b"m", &[0; 64], &[1; 32],
        );
        assert_eq!(
            fee_parts(&signed(&[precompile, transfer])).unwrap(),
            (10_000, 0)
        );

        // LiteSVM charges the signature fee only, so the debit restores bank parity.
        let mut vm = LiteSVM::new().with_blockhash_check(false);
        vm.airdrop(&payer.pubkey(), 1_000_000_000).unwrap();
        debit(&mut vm, &payer.pubkey(), 201).unwrap();
        vm.send_transaction(priced).unwrap();
        assert_eq!(
            vm.get_balance(&payer.pubkey()),
            Some(1_000_000_000 - 201 - 5_000 - 1_000)
        );
        assert!(debit(&mut vm, &Pubkey::new_unique(), 1).is_err());
    }
}
