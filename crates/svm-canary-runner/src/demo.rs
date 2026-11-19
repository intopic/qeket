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
use canary_types::{CanaryAttestationPayload, DOMAIN};
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
    for i in 0..=archive.replay_slots {
        clock(&mut source, archive.anchor_slot + i, 1000 + i as i64);
        archive.clocks.push(source.get_sysvar());
        for index in 0..=83u32 {
            let value = (i * 84 + u64::from(index) + 1).to_le_bytes();
            let instruction =
                Instruction::new_with_bytes(target, &value, vec![AccountMeta::new(counter, false)]);
            let tx = Transaction::new_signed_with_payer(
                &[instruction],
                Some(&payer.pubkey()),
                &[&payer],
                source.latest_blockhash(),
            );
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
        format_version: 1,
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
            domain_separator: DOMAIN,
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
        "quorum":"2/3","deployed_program_data_verified":true,"candidate_invoked":true,"proposal_closed":true,"policy_rotation_checked":true,"emergency_checked":true});
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
    let pending = Pubkey::find_program_address(&[b"policy", cfg.as_ref()], &ID).0;
    let mut new = policy.clone();
    new.threshold = 3;
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
    send(&mut vm, payer, &[gov], &[cancel, close.clone()])?;
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
