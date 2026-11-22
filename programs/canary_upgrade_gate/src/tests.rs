use super::*;
use anchor_lang::solana_program::{ed25519_program, instruction::Instruction};
use canary_types::DOMAIN;

fn key(byte: u8) -> Pubkey {
    Pubkey::new_from_array([byte; 32])
}

fn setup() -> (Policy, UpgradeProposal, CanaryAttestationPayload, Clock) {
    let policy = Policy {
        replay_mode: Default::default(),
        verifiers: [key(1), key(2), key(3), key(0), key(0), key(0), key(0)],
        verifier_count: 3,
        threshold: 2,
        max_cu_delta_bps: 1500,
        min_replay_txs: 250,
        min_replay_slots: 50,
        timelock_seconds: 60,
        proposal_expiry_seconds: 600,
    };
    let proposal = UpgradeProposal {
        bump: 255,
        nonce: 17,
        target_program: key(8),
        baseline_deploy_slot: 35,
        buffer_account: key(9),
        spill_account: key(10),
        expected_elf_sha256: [11; 32],
        git_commit_sha: [12; 20],
        anchor_slot: 100,
        replay_slots: 50,
        diff_root: [0; 32],
        attestation_bitmap: 0,
        attestation_count: 0,
        created_at: 400,
        expires_at: 1000,
        ready_at: 0,
        status: ProposalStatus::Pending,
    };
    let payload = CanaryAttestationPayload {
        domain_separator: DOMAIN,
        target_program: key(8),
        proposal_nonce: 17,
        buffer_account: key(9),
        anchor_slot: 100,
        replay_slots: 50,
        elf_sha256: [11; 32],
        diff_root: [13; 32],
        replayed_tx_count: 250,
        failed_tx_count: 0,
        max_cu_delta_bps: 1500,
        expires_at_slot: 160,
    };
    let clock = Clock {
        slot: 151,
        unix_timestamp: 500,
        ..Clock::default()
    };
    (policy, proposal, payload, clock)
}

#[test]
fn single_verifier_starts_timelock_with_one_valid_vote() {
    let (mut policy, mut proposal, payload, clock) = setup();
    policy.verifiers[1..].fill(Pubkey::default());
    policy.verifier_count = 1;
    policy.threshold = 1;
    policy.validate().unwrap();
    let execution_time = clock.unix_timestamp + policy.timelock_seconds;

    assert!(proposal
        .check_execution(&policy, execution_time, 35)
        .is_err());
    assert!(proposal
        .record_vote(&policy, &payload, key(2), &clock)
        .is_err());
    let mut invalid = payload.clone();
    invalid.failed_tx_count = 1;
    assert!(proposal
        .record_vote(&policy, &invalid, key(1), &clock)
        .is_err());
    assert_eq!(proposal.attestation_count, 0);

    proposal
        .record_vote(&policy, &payload, key(1), &clock)
        .unwrap();
    assert_eq!(proposal.attestation_count, 1);
    assert_eq!(proposal.status, ProposalStatus::Ready);
    assert!(proposal
        .check_execution(&policy, execution_time - 1, 35)
        .is_err());
    assert!(proposal
        .check_execution(&policy, execution_time, 35)
        .is_ok());
}

#[test]
fn distinct_matching_votes_start_timelock_at_quorum() {
    let (policy, mut proposal, payload, mut clock) = setup();
    proposal
        .record_vote(&policy, &payload, key(1), &clock)
        .unwrap();
    assert_eq!(proposal.status, ProposalStatus::Pending);
    assert!(proposal.check_execution(&policy, 999, 35).is_err());
    clock.unix_timestamp += 10;
    proposal
        .record_vote(&policy, &payload, key(3), &clock)
        .unwrap();
    assert_eq!(
        (
            proposal.status,
            proposal.ready_at,
            proposal.attestation_bitmap
        ),
        (ProposalStatus::Ready, 510, 5)
    );
    assert!(proposal.check_execution(&policy, 569, 35).is_err());
    assert!(proposal.check_execution(&policy, 570, 35).is_ok());
    assert!(proposal.check_execution(&policy, 570, 36).is_err());
    assert!(proposal.check_execution(&policy, 1000, 35).is_ok());
    assert!(proposal.check_execution(&policy, 1001, 35).is_err());
    assert!(proposal
        .record_vote(&policy, &payload, key(2), &clock)
        .is_err());
    for status in [ProposalStatus::Executed, ProposalStatus::Cancelled] {
        proposal.status = status;
        assert!(proposal.check_execution(&policy, 570, 35).is_err());
        assert!(proposal
            .record_vote(&policy, &payload, key(2), &clock)
            .is_err());
    }
}

#[test]
fn refused_votes_preserve_first_vote_and_root() {
    let (policy, mut proposal, payload, clock) = setup();
    proposal
        .record_vote(&policy, &payload, key(1), &clock)
        .unwrap();
    for verifier in [key(1), key(0), key(4)] {
        assert!(proposal
            .record_vote(&policy, &payload, verifier, &clock)
            .is_err());
    }
    let mut changed = payload.clone();
    changed.diff_root[0] ^= 1;
    assert!(proposal
        .record_vote(&policy, &changed, key(2), &clock)
        .is_err());
    assert_eq!(
        (
            proposal.attestation_count,
            proposal.attestation_bitmap,
            proposal.diff_root
        ),
        (1, 1, payload.diff_root)
    );
}

#[test]
fn each_proposal_binding_and_replay_limit_is_enforced() {
    let (policy, mut proposal, p, clock) = setup();
    let invalid = [
        CanaryAttestationPayload {
            domain_separator: [0; 16],
            ..p.clone()
        },
        CanaryAttestationPayload {
            target_program: key(42),
            ..p.clone()
        },
        CanaryAttestationPayload {
            proposal_nonce: 42,
            ..p.clone()
        },
        CanaryAttestationPayload {
            buffer_account: key(42),
            ..p.clone()
        },
        CanaryAttestationPayload {
            anchor_slot: 99,
            ..p.clone()
        },
        CanaryAttestationPayload {
            replay_slots: 51,
            ..p.clone()
        },
        CanaryAttestationPayload {
            elf_sha256: [0; 32],
            ..p.clone()
        },
        CanaryAttestationPayload {
            failed_tx_count: 1,
            ..p.clone()
        },
        CanaryAttestationPayload {
            replayed_tx_count: 249,
            ..p.clone()
        },
        CanaryAttestationPayload {
            max_cu_delta_bps: 1501,
            ..p.clone()
        },
        CanaryAttestationPayload {
            expires_at_slot: 150,
            ..p.clone()
        },
    ];
    for payload in invalid {
        assert!(proposal
            .record_vote(&policy, &payload, key(1), &clock)
            .is_err());
    }
    assert_eq!(proposal.attestation_count, 0);
}

#[test]
fn inclusive_replay_window_must_be_complete_and_proposal_live() {
    let (policy, mut proposal, p, mut clock) = setup();
    clock.slot = 150;
    assert!(proposal.record_vote(&policy, &p, key(1), &clock).is_err());
    clock.slot = 160;
    clock.unix_timestamp = 1001;
    assert!(proposal.record_vote(&policy, &p, key(1), &clock).is_err());
    clock.unix_timestamp = 999;
    assert!(proposal.record_vote(&policy, &p, key(1), &clock).is_ok());
    proposal.anchor_slot = u64::MAX;
    assert!(proposal.window_end().is_err());
}

#[test]
fn policy_checks_active_set_and_timing() {
    let (p, ..) = setup();
    p.validate().unwrap();
    for bad in [
        Policy {
            threshold: 0,
            ..p.clone()
        },
        Policy {
            threshold: 4,
            ..p.clone()
        },
        Policy {
            verifier_count: 8,
            ..p.clone()
        },
        Policy {
            verifier_count: 4,
            ..p.clone()
        },
        Policy {
            verifiers: [key(1); 7],
            ..p.clone()
        },
        Policy {
            min_replay_txs: 0,
            ..p.clone()
        },
        Policy {
            min_replay_slots: 0,
            ..p.clone()
        },
        Policy {
            timelock_seconds: 0,
            ..p.clone()
        },
        Policy {
            proposal_expiry_seconds: 60,
            ..p.clone()
        },
    ] {
        assert!(bad.validate().is_err());
    }
}

#[test]
fn payload_layout_and_precompile_envelope_are_stable() {
    let (_, _, payload, _) = setup();
    let message = payload.try_to_vec().unwrap();
    assert_eq!(message.len(), 186);
    assert_eq!(&message[..16], &DOMAIN);
    assert_eq!(&message[48..56], &17u64.to_le_bytes());
    assert_eq!(&message[178..], &160u64.to_le_bytes());
    // This unit test covers the message envelope. Signature math belongs to the runtime.
    let mut data = vec![
        1, 0, 48, 0, 255, 255, 16, 0, 255, 255, 112, 0, 186, 0, 255, 255,
    ];
    data.extend_from_slice(key(2).as_ref());
    data.extend_from_slice(&[0; 64]);
    data.extend_from_slice(&message);
    let ix = Instruction {
        program_id: ed25519_program::ID,
        accounts: vec![],
        data,
    };
    let (signer, decoded) = ed25519_check::signed_payload(&ix).unwrap();
    assert_eq!(signer, key(2));
    assert_eq!(decoded, payload);
    assert_eq!(
        ed25519_check::signer_from_instruction(&ix, &message).unwrap(),
        key(2)
    );
    for index in 0..16 {
        let mut bad = ix.clone();
        bad.data[index] ^= 1;
        assert!(ed25519_check::signer_from_instruction(&bad, &message).is_err());
    }
    for size in [0, 1, 15, 111, ix.data.len() - 1] {
        let mut bad = ix.clone();
        bad.data.truncate(size);
        assert!(ed25519_check::signer_from_instruction(&bad, &message).is_err());
    }
    let mut bad = ix.clone();
    bad.program_id = key(42);
    assert!(ed25519_check::signer_from_instruction(&bad, &message).is_err());
    let mut bad = ix;
    bad.data[112] ^= 1;
    assert!(ed25519_check::signer_from_instruction(&bad, &message).is_err());
}

#[test]
fn attestation_transaction_size() {
    use anchor_lang::solana_program::{message::Message, sysvar::instructions};
    use anchor_lang::{InstructionData, ToAccountMetas};
    let (_, _, payload, _) = setup();
    let verify = Instruction {
        program_id: ed25519_program::ID,
        accounts: vec![],
        data: vec![0; 112 + payload.try_to_vec().unwrap().len()],
    };
    let submit = Instruction {
        program_id: crate::ID,
        accounts: crate::accounts::SubmitCanaryAttestation {
            config: key(5),
            proposal: key(6),
            instruction_sysvar: instructions::ID,
        }
        .to_account_metas(None),
        data: crate::instruction::SubmitVerifiedAttestation {}.data(),
    };
    let message = Message::new(&[verify, submit], Some(&key(7)));
    assert_eq!(message.header.num_required_signatures, 1);
    let wire_bytes = 1 + 64 + message.serialize().len();
    println!("Attestation with one fee payer: {wire_bytes} bytes");
    // The worker builds this transaction byte for byte (scripts/solana_tx.py).
    assert!(wire_bytes < 650);
}

#[test]
fn loader_buffer_requires_bytes_and_the_gate_as_authority() {
    let buffer_key = key(7);
    let gate = key(9);
    let mut bytes = vec![1, 0, 0, 0, 1]; // Loader-v3 Buffer discriminant, then Some(pubkey).
    bytes.extend_from_slice(gate.as_ref());
    bytes.push(42);
    let check = |data: &mut [u8], authority| {
        let mut lamports = 1;
        let account = AccountInfo::new(
            &buffer_key,
            false,
            false,
            &mut lamports,
            data,
            &loader::ID,
            false,
            0,
        );
        check_buffer(&account, authority)
    };
    assert!(check(&mut bytes, gate).is_ok());
    assert!(check(&mut bytes, key(11)).is_err());
    assert!(check(&mut bytes[..37], gate).is_err());
    bytes[4] = 0;
    assert!(check(&mut bytes, gate).is_err());
    bytes[0] = 2;
    assert!(check(&mut bytes, gate).is_err());
}

#[test]
fn cpi_matches_loader_sdk() {
    let target = key(15);
    let data = loader::get_program_data_address(&target);
    assert_eq!(
        upgrade_ix(target, data, key(2), key(3), key(4)),
        loader::upgrade(&target, &key(2), &key(3), &key(4))
    );
}

#[test]
fn generation_changes_only_on_rotation() {
    let (policy, ..) = setup();
    let mut cfg = GatekeeperConfig {
        version: 1,
        bump: 0,
        target_program: key(8),
        governance_authority: key(5),
        emergency_authority: key(6),
        policy: policy.clone(),
        proposal_nonce: 0,
    };
    let original = cfg.next_nonce().unwrap();
    cfg.check_generation(original).unwrap();
    cfg.proposal_nonce = u64::from(u32::MAX);
    assert!(cfg.next_nonce().is_err());
    cfg.rotate(policy).unwrap();
    assert!(cfg.check_generation(original).is_err());
    assert_eq!(cfg.next_nonce().unwrap(), 1u64 << 32);
}

#[test]
fn signature_domains_bind_the_replay_mode() {
    let (mut policy, mut proposal, mut payload, clock) = setup();
    policy.replay_mode = ReplayMode::NoRevertAndCuBound;
    assert!(proposal
        .record_vote(&policy, &payload, key(1), &clock)
        .is_err());
    payload.domain_separator = policy.replay_mode.domain();
    proposal
        .record_vote(&policy, &payload, key(1), &clock)
        .unwrap();
}
