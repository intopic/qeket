use crate::replay::{hash, verify, Archive, Record};
use anchor_lang::{
    prelude::{Clock, Pubkey},
    solana_program::{
        bpf_loader_upgradeable as loader,
        instruction::{AccountMeta, Instruction},
        system_program, sysvar,
    },
    AccountDeserialize, AnchorSerialize, InstructionData, ToAccountMetas,
};
use anyhow::{anyhow, ensure, Context, Result};
use canary_types::CanaryAttestationPayload;
use canary_upgrade_gate::{
    accounts, instruction,
    state::{GatekeeperConfig, Policy, ProposalStatus, UpgradeProposal},
    ID,
};
use litesvm::{types::TransactionMetadata, LiteSVM};
use solana_account::Account;
use solana_keypair::Keypair;
use solana_signer::Signer;
use solana_transaction::Transaction;
use std::{fs, path::Path};

fn ix(accounts: impl ToAccountMetas, data: impl InstructionData) -> Instruction {
    Instruction {
        program_id: ID,
        accounts: accounts.to_account_metas(None),
        data: data.data(),
    }
}

fn send(
    vm: &mut LiteSVM,
    payer: &Keypair,
    extra: &[&Keypair],
    instructions: &[Instruction],
) -> Result<(usize, TransactionMetadata)> {
    // Independent test submissions must have different recent blockhashes, even on retries.
    vm.expire_blockhash();
    let mut keys = vec![payer];
    keys.extend_from_slice(extra);
    let tx = Transaction::new_signed_with_payer(
        instructions,
        Some(&payer.pubkey()),
        &keys,
        vm.latest_blockhash(),
    );
    let size = bincode::serialized_size(&tx)? as usize;
    let meta = vm
        .send_transaction(tx)
        .map_err(|e| anyhow!("{:?}: {}", e.err, e.meta.logs.join("\n")))?;
    Ok((size, meta))
}

fn rejected(result: Result<(usize, TransactionMetadata)>, reason: &str) -> Result<()> {
    let message = result
        .err()
        .context("gate unexpectedly accepted the operation")?
        .to_string();
    ensure!(message.contains(reason), "expected {reason}, got {message}");
    Ok(())
}

fn load<T: AccountDeserialize>(vm: &LiteSVM, key: Pubkey) -> Result<T> {
    let account = vm.get_account(&key).context("account missing")?;
    ensure!(account.owner == ID, "gate account has wrong owner");
    Ok(T::try_deserialize(&mut account.data.as_slice())?)
}

fn account(vm: &LiteSVM, owner: Pubkey, data: Vec<u8>, executable: bool) -> Account {
    Account {
        lamports: vm.minimum_balance_for_rent_exemption(data.len()),
        owner,
        data,
        executable,
        rent_epoch: 0,
    }
}

fn clock(vm: &mut LiteSVM, slot: u64, time: i64) {
    let mut c = vm.get_sysvar::<Clock>();
    c.slot = slot;
    c.unix_timestamp = time;
    vm.set_sysvar(&c);
}

fn traffic(target: Pubkey, baseline: &[u8], archive: &mut Archive) -> Result<()> {
    // This public seed identifies a synthetic fee payer in the replay fixture only.
    let payer = Keypair::new_from_array([72; 32]);
    let counter = Pubkey::new_from_array([73; 32]);
    let mut source = LiteSVM::new();
    source.add_program(target, baseline)?;
    archive.accounts = vec![
        (
            payer.pubkey(),
            Account {
                lamports: 10_000_000_000,
                ..Account::default()
            },
        ),
        (counter, account(&source, target, vec![0; 8], false)),
    ];
    for (key, a) in &archive.accounts {
        source.set_account(*key, a.clone())?;
    }
    let router = Pubkey::new_from_array([74; 32]);
    let programdata_address = loader::get_program_data_address(&router);
    let mut bytes = bincode::serialize(&loader::UpgradeableLoaderState::ProgramData {
        slot: 0,
        upgrade_authority_address: Some(payer.pubkey()),
    })?;
    bytes.extend_from_slice(baseline);
    let executable = bincode::serialize(&loader::UpgradeableLoaderState::Program {
        programdata_address,
    })?;
    let program_account = account(&source, loader::ID, executable, true);
    let data_account = account(&source, loader::ID, bytes, false);
    source.set_account(programdata_address, data_account.clone())?;
    source.set_account(router, program_account.clone())?;
    // Archive ordering deliberately differs from loader initialization order.
    archive.accounts.extend([
        (router, program_account),
        (programdata_address, data_account),
    ]);
    let table_key = Pubkey::new_from_array([75; 32]);
    let addresses = vec![counter, target];
    use anchor_lang::solana_program::address_lookup_table::state::{
        AddressLookupTable, LookupTableMeta,
    };
    let table = AddressLookupTable {
        meta: LookupTableMeta::default(),
        addresses: (&addresses[..]).into(),
    };
    let table_account = account(
        &source,
        solana_sdk_ids::address_lookup_table::ID,
        table.serialize_for_tests()?,
        false,
    );
    source.set_account(table_key, table_account.clone())?;
    archive.accounts.push((table_key, table_account));
    let lookup = solana_message::AddressLookupTableAccount {
        key: table_key,
        addresses,
    };
    for i in 0..=archive.replay_slots {
        clock(&mut source, archive.anchor_slot + i, 1000 + i as i64);
        archive.clocks.push(source.get_sysvar());
        for index in 0..=83u32 {
            let value = (i * 84 + u64::from(index) + 1).to_le_bytes();
            let instruction =
                Instruction::new_with_bytes(target, &value, vec![AccountMeta::new(counter, false)]);
            let tx = if index == 0 {
                let router_ix = Instruction::new_with_bytes(
                    router,
                    &value,
                    vec![
                        AccountMeta::new(counter, false),
                        AccountMeta::new_readonly(target, false),
                    ],
                );
                let budget = solana_compute_budget_interface::ComputeBudgetInstruction::set_compute_unit_limit(150_000);
                let msg = solana_message::v0::Message::try_compile(
                    &payer.pubkey(),
                    &[budget, router_ix],
                    std::slice::from_ref(&lookup),
                    source.latest_blockhash(),
                )?;
                solana_transaction::versioned::VersionedTransaction::try_new(
                    solana_message::VersionedMessage::V0(msg),
                    &[&payer],
                )?
            } else {
                Transaction::new_signed_with_payer(
                    &[instruction],
                    Some(&payer.pubkey()),
                    &[&payer],
                    source.latest_blockhash(),
                )
                .into()
            };
            source
                .send_transaction(tx.clone())
                .map_err(|e| anyhow!("capturing baseline: {e:?}"))?;
            archive.records.push(Record {
                slot: archive.anchor_slot + i,
                index,
                transaction: tx,
            });
        }
    }
    archive.transaction_manifest = crate::replay::manifest(&archive.records)?;
    Ok(())
}

pub fn run(
    gate: &[u8],
    baseline: &[u8],
    candidate: &[u8],
    rebuilt: &[u8],
    commit: &str,
    out: &Path,
) -> Result<()> {
    let commit_bytes: [u8; 20] = hex::decode(commit)?
        .try_into()
        .map_err(|_| anyhow!("expected a Git SHA-1"))?;
    ensure!(candidate != baseline, "candidate must be a different build");
    let mut vm = LiteSVM::new();
    let payer = Keypair::new();
    let governance = Keypair::new();
    let emergency = Keypair::new();
    let nodes = [Keypair::new(), Keypair::new(), Keypair::new()];
    vm.airdrop(&payer.pubkey(), 100_000_000_000)
        .map_err(|e| anyhow!("{e:?}"))?;
    vm.add_program(ID, gate)?;
    let target = Pubkey::new_from_array([58; 32]);
    let pd = loader::get_program_data_address(&target);
    let buffer = Pubkey::new_unique();
    let cfg = Pubkey::find_program_address(&[b"gatekeeper_cfg", target.as_ref()], &ID).0;
    let proposal =
        Pubkey::find_program_address(&[b"proposal", target.as_ref(), &0u64.to_le_bytes()], &ID).0;
    let mut pdata = bincode::serialize(&loader::UpgradeableLoaderState::ProgramData {
        slot: 1,
        upgrade_authority_address: Some(governance.pubkey()),
    })?;
    pdata.extend_from_slice(baseline);
    pdata.resize(
        loader::UpgradeableLoaderState::size_of_programdata_metadata()
            + baseline.len().max(candidate.len() + 16),
        0,
    );
    vm.set_account(pd, account(&vm, loader::ID, pdata, false))?;
    let program = bincode::serialize(&loader::UpgradeableLoaderState::Program {
        programdata_address: pd,
    })?;
    vm.set_account(target, account(&vm, loader::ID, program, true))?;
    let mut buf = bincode::serialize(&loader::UpgradeableLoaderState::Buffer {
        authority_address: Some(governance.pubkey()),
    })?;
    buf.extend_from_slice(candidate);
    buf.extend_from_slice(&[0; 16]);
    vm.set_account(buffer, account(&vm, loader::ID, buf, false))?;
    clock(&mut vm, 100, 1000);
    let mut verifiers = [Pubkey::default(); 7];
    for (slot, node) in verifiers.iter_mut().zip(&nodes) {
        *slot = node.pubkey();
    }
    let policy = Policy {
        replay_mode: Default::default(),
        verifiers,
        verifier_count: 3,
        threshold: 2,
        max_cu_delta_bps: 1500,
        min_replay_txs: 250,
        min_replay_slots: 2,
        timelock_seconds: 60,
        proposal_expiry_seconds: 600,
    };
    send(
        &mut vm,
        &payer,
        &[&governance],
        &[ix(
            accounts::InitializeGatekeeper {
                payer: payer.pubkey(),
                governance_authority: governance.pubkey(),
                target_program: target,
                program_data: pd,
                config: cfg,
                system_program: system_program::ID,
            },
            instruction::InitializeGatekeeper {
                policy: policy.clone(),
                emergency_authority: emergency.pubkey(),
            },
        )],
    )?;
    send(
        &mut vm,
        &payer,
        &[&governance],
        &[
            loader::set_upgrade_authority(&target, &governance.pubkey(), Some(&cfg)),
            loader::set_buffer_authority(&buffer, &governance.pubkey(), &cfg),
        ],
    )?;
    send(
        &mut vm,
        &payer,
        &[&governance],
        &[ix(
            accounts::ProposeBufferUpgrade {
                payer: payer.pubkey(),
                governance_authority: governance.pubkey(),
                config: cfg,
                program_data: pd,
                buffer,
                proposal,
                reservation: Pubkey::find_program_address(
                    &[b"buffer", cfg.as_ref(), buffer.as_ref()],
                    &ID,
                )
                .0,
                system_program: system_program::ID,
            },
            instruction::ProposeBufferUpgrade {
                spill: payer.pubkey(),
                expected_elf_sha256: hash(candidate),
                git_commit_sha: commit_bytes,
                anchor_slot: None,
                replay_slots: 2,
            },
        )],
    )?;
    let p: UpgradeProposal = load(&vm, proposal)?;
    let config: GatekeeperConfig = load(&vm, cfg)?;
    let live_baseline = vm.get_account(&pd).unwrap();
    ensure!(
        live_baseline.data[loader::UpgradeableLoaderState::size_of_programdata_metadata()..]
            .starts_with(baseline),
        "wrong base program"
    );
    let mut archive = Archive {
        format_version: 2,
        replay_mode: config.policy.replay_mode,
        slot_hashes: vec![],
        absent_accounts: vec![],
        transaction_manifest: [0; 32],
        target: p.target_program,
        gate: cfg,
        buffer_key: p.buffer_account,
        nonce: p.nonce,
        source_commit: hex::encode(p.git_commit_sha),
        expected_elf: p.expected_elf_sha256,
        baseline_elf: hash(baseline),
        buffer: vm.get_account(&buffer).unwrap(),
        anchor_slot: p.anchor_slot,
        replay_slots: p.replay_slots,
        min_txs: config.policy.min_replay_txs,
        cu_limit_bps: config.policy.max_cu_delta_bps,
        accounts: vec![],
        clocks: vec![],
        records: vec![],
    };
    traffic(target, baseline, &mut archive)?;
    check_replay_modes(&archive, baseline, candidate)?;
    clock(&mut vm, 103, 1010);
    let execute = ix(
        accounts::ExecuteGatedUpgrade {
            config: cfg,
            proposal,
            target_program: target,
            program_data: pd,
            buffer,
            spill: payer.pubkey(),
            rent: sysvar::rent::ID,
            clock: sysvar::clock::ID,
            loader_program: loader::ID,
        },
        instruction::ExecuteGatedUpgrade {},
    );
    let submit = ix(
        accounts::SubmitCanaryAttestation {
            config: cfg,
            proposal,
            instruction_sysvar: sysvar::instructions::ID,
        },
        instruction::SubmitVerifiedAttestation {},
    );
    let mut measured = Vec::new();
    let mut consensus = None;
    let mut vote_message = None;
    for (node, local_build) in nodes[..2].iter().zip([candidate, rebuilt]) {
        let evidence = verify(&archive, baseline, local_build)?;
        if let Some(previous) = &consensus {
            ensure!(previous == &evidence, "verifiers disagree");
        }
        let payload = CanaryAttestationPayload {
            domain_separator: evidence.replay_mode.domain(),
            target_program: target,
            proposal_nonce: p.nonce,
            buffer_account: buffer,
            anchor_slot: p.anchor_slot,
            replay_slots: p.replay_slots,
            elf_sha256: evidence.elf_sha256,
            diff_root: evidence.diff_root,
            replayed_tx_count: evidence.replayed_tx_count,
            failed_tx_count: 0,
            max_cu_delta_bps: evidence.max_cu_delta_bps,
            expires_at_slot: 150,
        };
        let bytes = payload.try_to_vec()?;
        let signature: [u8; 64] = node.sign_message(&bytes).into();
        let verify_ix = solana_ed25519_program::new_ed25519_instruction_with_signature(
            &bytes,
            &signature,
            &node.pubkey().to_bytes(),
        );
        let vote = [verify_ix, submit.clone()];
        let (size, meta) = send(&mut vm, &payer, &[], &vote)?;
        if vote_message.is_none() {
            let message =
                anchor_lang::solana_program::message::Message::new(&vote, Some(&payer.pubkey()));
            vote_message = Some(message.serialize());
        }
        ensure!(
            size < 650 && meta.compute_units_consumed < 25_000,
            "attestation exceeded budget: {size} B / {} CU",
            meta.compute_units_consumed
        );
        measured.push((size, meta.compute_units_consumed));
        if consensus.is_none() {
            rejected(send(&mut vm, &payer, &[], &vote), "AlreadyVoted")?;
            rejected(
                send(&mut vm, &payer, &[], std::slice::from_ref(&execute)),
                "WrongStatus",
            )?;
        }
        consensus = Some(evidence);
    }
    let ready: UpgradeProposal = load(&vm, proposal)?;
    ensure!(
        ready.status == ProposalStatus::Ready && ready.attestation_count == 2,
        "quorum missing"
    );
    rejected(
        send(&mut vm, &payer, &[], std::slice::from_ref(&execute)),
        "Timelocked",
    )?;
    let branch = vm.clone();
    clock(&mut vm, 110, 1070);
    let (execution_bytes, meta) = send(&mut vm, &payer, &[], std::slice::from_ref(&execute))?;
    let final_state: UpgradeProposal = load(&vm, proposal)?;
    let deployed = vm.get_account(&pd).unwrap();
    let body = &deployed.data[loader::UpgradeableLoaderState::size_of_programdata_metadata()..];
    ensure!(
        body.starts_with(rebuilt)
            && body[rebuilt.len()..].iter().all(|b| *b == 0)
            && final_state.status == ProposalStatus::Executed,
        "deployed bytecode differs"
    );
    clock(&mut vm, 111, 1071);
    let counter = Pubkey::new_unique();
    vm.set_account(counter, account(&vm, target, vec![0; 8], false))?;
    let (_, invoked) = send(
        &mut vm,
        &payer,
        &[],
        &[Instruction::new_with_bytes(
            target,
            &7u64.to_le_bytes(),
            vec![AccountMeta::new(counter, false)],
        )],
    )?;
    ensure!(
        invoked.logs.iter().any(|line| line.contains("counter v2"))
            && vm.get_account(&counter).unwrap().data == 7u64.to_le_bytes(),
        "candidate behavior missing"
    );
    let close = ix(
        accounts::CloseProposal {
            reservation: Pubkey::find_program_address(
                &[b"buffer", cfg.as_ref(), buffer.as_ref()],
                &ID,
            )
            .0,
            governance_authority: governance.pubkey(),
            config: cfg,
            proposal,
            spill: payer.pubkey(),
        },
        instruction::CloseProposal {},
    );
    send(
        &mut vm,
        &payer,
        &[&governance],
        std::slice::from_ref(&close),
    )?;
    ensure!(
        vm.get_account(&proposal).is_none(),
        "proposal rent not reclaimed"
    );
    check_governance(
        branch,
        &payer,
        &governance,
        &emergency,
        cfg,
        proposal,
        target,
        pd,
        buffer,
        &policy,
        &execute,
        &close,
    )?;
    ensure!(
        execution_bytes < 650 && meta.compute_units_consumed < 25_000,
        "upgrade exceeded budget: {execution_bytes} B / {} CU",
        meta.compute_units_consumed
    );
    let evidence = consensus.context("missing quorum evidence")?;
    let archive_bytes = serde_json::to_vec(&archive)?;
    let report = serde_json::json!({"mode":"local-litesvm","source_commit":commit,"candidate_sha256":hex::encode(evidence.elf_sha256),
        "archive_sha256":hex::encode(hash(&archive_bytes)), "gate_sha256":hex::encode(hash(gate)),
        "diff_root":hex::encode(evidence.diff_root),"replayed_transactions_per_verifier":evidence.replayed_tx_count,"max_cu_delta_bps":evidence.max_cu_delta_bps,
        "attestations_bytes_and_cu":measured,"execution_bytes":execution_bytes,"execution_cu":meta.compute_units_consumed,
        "reclaim_cancelled_and_expired_checked":true, "authority_rotation_and_exit_checked":true,
        "v0_alt_cpi_transactions":archive.records.iter().filter(|record| matches!(record.transaction.message, solana_message::VersionedMessage::V0(_))).count(),
        "relaxed_mode_checked":true, "quorum":"2/3","deployed_program_data_verified":true,"candidate_invoked":true,"proposal_closed":true,"policy_rotation_checked":true,"emergency_checked":true});
    fs::create_dir_all(out)?;
    fs::write(
        out.join("vote-message.bin"),
        vote_message.context("missing verified vote")?,
    )?;
    fs::write(out.join("archive.json"), archive_bytes)?;
    fs::write(out.join("report.json"), serde_json::to_vec_pretty(&report)?)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn check_governance(
    mut vm: LiteSVM,
    payer: &Keypair,
    gov: &Keypair,
    emergency: &Keypair,
    cfg: Pubkey,
    proposal: Pubkey,
    target: Pubkey,
    pd: Pubkey,
    buffer: Pubkey,
    policy: &Policy,
    execute: &Instruction,
    close: &Instruction,
) -> Result<()> {
    check_authorities(&vm, payer, gov, emergency, cfg, pd)?;
    let pending = Pubkey::find_program_address(&[b"policy", cfg.as_ref()], &ID).0;
    let mut new = policy.clone();
    new.threshold = 3;
    new.proposal_expiry_seconds += 500;
    let queue = ix(
        accounts::QueuePolicy {
            payer: payer.pubkey(),
            governance_authority: gov.pubkey(),
            config: cfg,
            pending,
            system_program: system_program::ID,
        },
        instruction::ProposeConfigUpdate { policy: new },
    );
    send(&mut vm, payer, &[gov], &[queue])?;
    let apply = ix(
        accounts::ResolvePolicy {
            governance_authority: gov.pubkey(),
            config: cfg,
            pending,
            refund: payer.pubkey(),
        },
        instruction::ExecuteConfigUpdate {},
    );
    rejected(
        send(&mut vm, payer, &[gov], std::slice::from_ref(&apply)),
        "Timelocked",
    )?;
    clock(&mut vm, 112, 1071);
    send(&mut vm, payer, &[gov], &[apply])?;
    rejected(
        send(&mut vm, payer, &[], std::slice::from_ref(execute)),
        "PolicyChanged",
    )?;
    let cancel = ix(
        accounts::CancelProposal {
            governance_authority: gov.pubkey(),
            config: cfg,
            proposal,
        },
        instruction::CancelProposal {},
    );
    let reclaim = ix(
        accounts::ReclaimBuffer {
            governance_authority: gov.pubkey(),
            config: cfg,
            proposal,
            buffer,
            spill: payer.pubkey(),
            loader_program: loader::ID,
        },
        instruction::ReclaimBuffer {},
    );
    rejected(
        send(&mut vm, payer, &[gov], std::slice::from_ref(&reclaim)),
        "WrongStatus",
    )?;
    let mut expired = vm.clone();
    clock(&mut expired, 700, 1600);
    rejected(
        send(&mut expired, payer, &[gov], std::slice::from_ref(&reclaim)),
        "WrongStatus",
    )?;
    clock(&mut expired, 701, 1601);
    send(&mut expired, payer, &[gov], std::slice::from_ref(&reclaim))?;
    ensure!(
        expired.get_account(&buffer).is_none(),
        "expired buffer still exists"
    );
    let mut cancelled = vm.clone();
    send(&mut cancelled, payer, &[gov], std::slice::from_ref(&cancel))?;
    rejected(
        send(&mut cancelled, payer, &[gov], std::slice::from_ref(close)),
        "WrongStatus",
    )?;
    let before = cancelled.get_balance(&payer.pubkey()).unwrap();
    let rent = cancelled.get_balance(&buffer).unwrap();
    let (_, receipt) = send(
        &mut cancelled,
        payer,
        &[gov],
        std::slice::from_ref(&reclaim),
    )?;
    ensure!(
        cancelled.get_balance(&payer.pubkey()) == Some(before + rent - 10_000)
            && cancelled.get_account(&buffer).is_none()
            && !receipt.inner_instructions.is_empty(),
        "buffer rent refund mismatch"
    );
    rejected(
        send(
            &mut cancelled,
            payer,
            &[gov],
            std::slice::from_ref(&reclaim),
        ),
        "WrongStatus",
    )?;
    send(&mut cancelled, payer, &[gov], std::slice::from_ref(close))?;
    send(&mut vm, payer, &[gov], &[cancel])?;
    let recover = |signer| {
        ix(
            accounts::EmergencyUpgrade {
                emergency_authority: signer,
                config: cfg,
                target_program: target,
                program_data: pd,
                buffer,
                spill: payer.pubkey(),
                rent: sysvar::rent::ID,
                clock: sysvar::clock::ID,
                loader_program: loader::ID,
            },
            instruction::EmergencyOverrideUpgrade {},
        )
    };
    rejected(
        send(&mut vm, payer, &[gov], &[recover(gov.pubkey())]),
        "ConstraintHasOne",
    )?;
    let (_, meta) = send(&mut vm, payer, &[emergency], &[recover(emergency.pubkey())])?;
    ensure!(
        meta.logs
            .iter()
            .any(|log| log.contains("EmergencyOverrideUpgrade"))
            && vm.get_account(&buffer).is_none(),
        "emergency upgrade missing"
    );
    Ok(())
}

fn check_authorities(
    base: &LiteSVM,
    payer: &Keypair,
    gov: &Keypair,
    emergency: &Keypair,
    cfg: Pubkey,
    pd: Pubkey,
) -> Result<()> {
    use canary_upgrade_gate::state::AuthorityRole;
    let next = Keypair::new();
    let pending = Pubkey::find_program_address(&[b"authority", cfg.as_ref()], &ID).0;
    let resolve = || accounts::ResolveAuthority {
        governance_authority: gov.pubkey(),
        config: cfg,
        pending,
        refund: payer.pubkey(),
    };
    let transfer = |signer, pending| {
        ix(
            accounts::TransferProgramAuthority {
                authority: signer,
                config: cfg,
                candidate: next.pubkey(),
                program_data: pd,
                loader_program: loader::ID,
                pending,
            },
            instruction::TransferProgramAuthority {},
        )
    };
    let check_exit = |vm: &LiteSVM| -> Result<()> {
        let raw = vm.get_account(&pd).context("missing ProgramData")?;
        ensure!(
            matches!(bincode::deserialize(&raw.data)?, loader::UpgradeableLoaderState::ProgramData { upgrade_authority_address: Some(k), .. } if k == next.pubkey()),
            "loader authority was not transferred"
        );
        Ok(())
    };
    let mut vm = base.clone();
    rejected(
        send(
            &mut vm,
            payer,
            &[gov, &next],
            &[transfer(gov.pubkey(), None)],
        ),
        "WrongAuthority",
    )?;
    send(
        &mut vm,
        payer,
        &[emergency, &next],
        &[transfer(emergency.pubkey(), None)],
    )?;
    check_exit(&vm)?;
    for role in [
        AuthorityRole::Governance,
        AuthorityRole::Emergency,
        AuthorityRole::ProgramUpgrade,
    ] {
        let mut vm = base.clone();
        let queue = ix(
            accounts::QueueAuthority {
                payer: payer.pubkey(),
                governance_authority: gov.pubkey(),
                config: cfg,
                pending,
                system_program: system_program::ID,
            },
            instruction::ProposeAuthorityChange {
                role,
                candidate: next.pubkey(),
            },
        );
        send(&mut vm, payer, &[gov], &[queue])?;
        let apply = if role == AuthorityRole::ProgramUpgrade {
            transfer(gov.pubkey(), Some(pending))
        } else {
            ix(
                accounts::RotateAuthority {
                    change: resolve(),
                    candidate: next.pubkey(),
                },
                instruction::ExecuteAuthorityChange {},
            )
        };
        clock(&mut vm, 110, 1069);
        rejected(
            send(&mut vm, payer, &[gov, &next], std::slice::from_ref(&apply)),
            "Timelocked",
        )?;
        clock(&mut vm, 111, 1070);
        send(&mut vm, payer, &[gov, &next], &[apply])?;
        let config: GatekeeperConfig = load(&vm, cfg)?;
        ensure!(
            config.proposal_nonce >> 32 == 1,
            "old generation remained active"
        );
        match role {
            AuthorityRole::Governance => ensure!(config.governance_authority == next.pubkey()),
            AuthorityRole::Emergency => ensure!(config.emergency_authority == next.pubkey()),
            AuthorityRole::ProgramUpgrade => {
                check_exit(&vm)?;
                send(
                    &mut vm,
                    payer,
                    &[gov],
                    &[ix(resolve(), instruction::CancelAuthorityChange {})],
                )?;
            }
        }
        ensure!(vm.get_account(&pending).is_none());
    }
    Ok(())
}

fn check_replay_modes(captured: &Archive, baseline: &[u8], candidate: &[u8]) -> Result<()> {
    use canary_upgrade_gate::state::ReplayMode;
    let mut archive = captured.clone();
    archive.min_txs = 1;
    archive.records.truncate(1);
    let payer = Keypair::new_from_array([72; 32]);
    let counter = Pubkey::new_from_array([73; 32]);
    let change = |program| {
        Transaction::new_signed_with_payer(
            &[Instruction::new_with_bytes(
                program,
                &0u64.to_le_bytes(),
                vec![AccountMeta::new(counter, false)],
            )],
            Some(&payer.pubkey()),
            &[&payer],
            LiteSVM::new().latest_blockhash(),
        )
        .into()
    };
    archive.records[0].transaction = change(archive.target);
    archive.transaction_manifest = crate::replay::manifest(&archive.records)?;
    let check_error = |archive: &Archive, reason: &str| -> Result<()> {
        let err = verify(archive, baseline, candidate)
            .err()
            .context("unexpected successful replay")?;
        ensure!(
            err.to_string().contains(reason),
            "expected {reason}, got {err}"
        );
        Ok(())
    };
    archive.cu_limit_bps = u16::MAX;
    check_error(&archive, "post-state differs")?;
    archive.replay_mode = ReplayMode::NoRevertAndCuBound;
    let first = verify(&archive, baseline, candidate)?;
    ensure!(
        first == verify(&archive, baseline, candidate)?,
        "non-deterministic relaxed evidence"
    );
    let mut unchanged = archive.clone();
    unchanged.expected_elf = hash(baseline);
    unchanged
        .buffer
        .data
        .truncate(loader::UpgradeableLoaderState::size_of_buffer_metadata());
    unchanged.buffer.data.extend_from_slice(baseline);
    let reference = verify(&unchanged, baseline, baseline)?;
    ensure!(
        first.diff_root != reference.diff_root,
        "relaxed root must commit to candidate state"
    );
    archive.cu_limit_bps = 0;
    check_error(&archive, "compute policy exceeded")?;
    archive.cu_limit_bps = u16::MAX;
    let counter_state = archive
        .accounts
        .iter_mut()
        .find(|(k, _)| *k == counter)
        .context("missing counter")?;
    counter_state.1.data = u64::MAX.to_le_bytes().to_vec();
    check_error(&archive, "candidate failed")?;
    // A successful transaction that simply lists the target cannot establish coverage.
    let mut unrelated =
        solana_compute_budget_interface::ComputeBudgetInstruction::set_compute_unit_limit(80_000);
    unrelated
        .accounts
        .push(AccountMeta::new_readonly(archive.target, false));
    archive.records[0].transaction = Transaction::new_signed_with_payer(
        &[unrelated],
        Some(&payer.pubkey()),
        &[&payer],
        LiteSVM::new().latest_blockhash(),
    )
    .into();
    archive.transaction_manifest = crate::replay::manifest(&archive.records)?;
    check_error(&archive, "target was not invoked")?;
    archive.records[0] = captured.records[0].clone();
    archive.transaction_manifest = crate::replay::manifest(&archive.records)?;
    archive
        .accounts
        .retain(|(_, a)| a.owner != solana_sdk_ids::address_lookup_table::ID);
    check_error(&archive, "missing lookup table")
}
