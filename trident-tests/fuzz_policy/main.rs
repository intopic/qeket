use anchor_lang::{
    solana_program::bpf_loader_upgradeable as loader, AccountDeserialize, AccountSerialize,
    InstructionData, ToAccountMetas,
};
use canary_upgrade_gate::{
    accounts, instruction,
    state::{
        AuthorityRole, GatekeeperConfig, PendingAuthority, Policy, ProposalStatus, ReplayMode,
        UpgradeProposal,
    },
    ID,
};
use trident_fuzz::fuzzing::*;

#[derive(FuzzTestMethods)]
struct FuzzTest {
    trident: Trident,
    fuzz_accounts: FuzzAccounts,
}

#[derive(Default)]
struct FuzzAccounts {
    governance: Pubkey,
    config: Pubkey,
    pending: Pubkey,
    target: Pubkey,
}

fn policy() -> Policy {
    let mut verifiers = [Pubkey::default(); 7];
    for (index, verifier) in verifiers.iter_mut().take(3).enumerate() {
        *verifier = Pubkey::new_from_array([index as u8 + 1; 32]);
    }
    Policy {
        replay_mode: ReplayMode::StrictEquality,
        verifiers,
        verifier_count: 3,
        threshold: 2,
        max_cu_delta_bps: 1500,
        min_replay_txs: 1,
        min_replay_slots: 2,
        timelock_seconds: 30,
        proposal_expiry_seconds: 90,
    }
}

#[flow_executor]
impl FuzzTest {
    fn new() -> Self {
        Self {
            trident: Trident::default(),
            fuzz_accounts: FuzzAccounts::default(),
        }
    }

    #[init]
    fn seed_config(&mut self) {
        let target = Pubkey::new_from_array([60; 32]);
        let (config, bump) =
            Pubkey::find_program_address(&[b"gatekeeper_cfg", target.as_ref()], &ID);
        let (pending, _) = Pubkey::find_program_address(&[b"policy", config.as_ref()], &ID);
        self.fuzz_accounts.governance = Pubkey::new_from_array([61; 32]);
        self.fuzz_accounts.config = config;
        self.fuzz_accounts.target = target;
        self.fuzz_accounts.pending = pending;
        self.trident
            .airdrop(&self.fuzz_accounts.governance, LAMPORTS_PER_SOL);
        let config = GatekeeperConfig {
            version: 2,
            bump,
            target_program: target,
            governance_authority: self.fuzz_accounts.governance,
            emergency_authority: Pubkey::new_from_array([62; 32]),
            policy: policy(),
            proposal_nonce: 0,
        };
        let mut data = Vec::new();
        config.try_serialize(&mut data).unwrap();
        let mut account = AccountSharedData::new(LAMPORTS_PER_SOL, data.len(), &ID);
        account.set_data_from_slice(&data);
        self.trident
            .set_account_custom(&self.fuzz_accounts.config, &account);
    }

    #[flow]
    fn exercise_gate(&mut self) {
        match scenario() {
            Scenario::Policy => self.validate_policy_updates(),
            Scenario::Reclaim => self.validate_buffer_reclaim(),
            Scenario::ReclaimOrphan => self.validate_orphan_buffer_reclaim(),
            Scenario::Transfer => self.validate_authority_transfer(),
            Scenario::Execute => self.validate_upgrade_execution_guards(),
        }
    }

    fn validate_policy_updates(&mut self) {
        let mut next = policy();
        next.verifier_count = self.trident.random_from_range(0..=8);
        next.threshold = self.trident.random_from_range(0..=7);
        next.min_replay_txs = self.trident.random_from_range(0..=100);
        next.min_replay_slots = self.trident.random_from_range(0..=40);
        next.timelock_seconds = self.trident.random_from_range(-4..=60);
        next.proposal_expiry_seconds = self.trident.random_from_range(-4..=120);
        if self.trident.random_from_range(0..=9) == 0 {
            next.verifiers[1] = next.verifiers[0];
        }
        let active = next.verifiers.get(..usize::from(next.verifier_count));
        let valid = (1..=next.verifier_count).contains(&next.threshold)
            && next.min_replay_txs > 0
            && next.min_replay_slots > 0
            && next.timelock_seconds > 0
            && next.proposal_expiry_seconds > next.timelock_seconds
            && active.is_some_and(|keys| {
                keys.iter()
                    .enumerate()
                    .all(|(index, key)| *key != Pubkey::default() && !keys[..index].contains(key))
            });
        let update = Instruction {
            program_id: ID,
            accounts: accounts::QueuePolicy {
                payer: self.fuzz_accounts.governance,
                governance_authority: self.fuzz_accounts.governance,
                config: self.fuzz_accounts.config,
                pending: self.fuzz_accounts.pending,
                system_program: Pubkey::default(),
            }
            .to_account_metas(None),
            data: instruction::ProposeConfigUpdate { policy: next }.data(),
        };
        let before = self.trident.get_account(&self.fuzz_accounts.config);
        let result = self
            .trident
            .process_transaction(&[update], Some("propose_config_update"));
        assert_eq!(result.is_success(), valid);
        assert_eq!(
            self.trident.get_account(&self.fuzz_accounts.config).data(),
            before.data()
        );
        assert_eq!(
            self.trident
                .get_account(&self.fuzz_accounts.pending)
                .owner()
                == &ID,
            valid
        );
    }

    fn validate_orphan_buffer_reclaim(&mut self) {
        let addresses = &self.fuzz_accounts;
        let buffer = Pubkey::new_from_array([63; 32]);
        let recipient = Pubkey::new_from_array([64; 32]);
        let (reservation, _) = Pubkey::find_program_address(
            &[b"buffer", addresses.config.as_ref(), buffer.as_ref()],
            &ID,
        );
        let reserved = self.trident.random_from_range(0..=1) == 1;
        let gate_authority = self.trident.random_from_range(0..=1) == 1;
        let same_spill = self.trident.random_from_range(0..=1) == 1;
        let spill = if same_spill { buffer } else { recipient };
        let authority = if gate_authority {
            addresses.config
        } else {
            addresses.governance
        };
        let state = loader::UpgradeableLoaderState::Buffer {
            authority_address: Some(authority),
        };
        let mut data = bincode::serialize(&state).unwrap();
        data.extend_from_slice(&[0; 16]);
        let mut account = AccountSharedData::new(LAMPORTS_PER_SOL, data.len(), &loader::ID);
        account.set_data_from_slice(&data);
        self.trident.set_account_custom(&buffer, &account);
        self.trident.airdrop(&recipient, LAMPORTS_PER_SOL);
        if reserved {
            self.trident.set_account_custom(
                &reservation,
                &AccountSharedData::new(LAMPORTS_PER_SOL, 8, &ID),
            );
        }
        let instruction = Instruction {
            program_id: ID,
            accounts: accounts::ReclaimOrphanBuffer {
                governance_authority: addresses.governance,
                config: addresses.config,
                buffer,
                reservation,
                spill,
                loader_program: loader::ID,
            }
            .to_account_metas(None),
            data: instruction::ReclaimOrphanBuffer {}.data(),
        };
        let valid = !reserved && gate_authority && !same_spill;
        let result = self
            .trident
            .process_transaction(&[instruction], Some("reclaim_orphan_buffer"));
        assert_eq!(result.is_success(), valid, "{:?}", result.logs());
        assert_eq!(
            self.trident.get_account(&buffer).lamports(),
            if valid { 0 } else { LAMPORTS_PER_SOL }
        );
        assert_eq!(
            self.trident.get_account(&recipient).lamports(),
            if valid {
                2 * LAMPORTS_PER_SOL
            } else {
                LAMPORTS_PER_SOL
            }
        );
    }

    fn validate_buffer_reclaim(&mut self) {
        let addresses = &self.fuzz_accounts;
        let buffer = Pubkey::new_from_array([63; 32]);
        let spill = Pubkey::new_from_array([64; 32]);
        let (proposal, bump) = Pubkey::find_program_address(
            &[b"proposal", addresses.target.as_ref(), &0u64.to_le_bytes()],
            &ID,
        );
        let status = [
            ProposalStatus::Pending,
            ProposalStatus::Ready,
            ProposalStatus::Executed,
            ProposalStatus::Cancelled,
            ProposalStatus::Reclaimed,
        ][self.trident.random_from_range(0..5)];
        let expires_offset: i64 = self.trident.random_from_range(-2..=2);
        let now = self.trident.get_current_timestamp();
        let state = UpgradeProposal {
            bump,
            nonce: 0,
            target_program: addresses.target,
            baseline_deploy_slot: 0,
            buffer_account: buffer,
            spill_account: spill,
            expected_elf_sha256: [0; 32],
            git_commit_sha: [0; 20],
            anchor_slot: 10,
            replay_slots: 2,
            diff_root: [0; 32],
            attestation_bitmap: 0,
            attestation_count: 0,
            created_at: now - 50,
            expires_at: now + expires_offset,
            ready_at: 0,
            status,
        };
        let mut data = Vec::new();
        state.try_serialize(&mut data).unwrap();
        let mut account = AccountSharedData::new(LAMPORTS_PER_SOL, data.len(), &ID);
        account.set_data_from_slice(&data);
        self.trident.set_account_custom(&proposal, &account);
        let state = loader::UpgradeableLoaderState::Buffer {
            authority_address: Some(addresses.config),
        };
        let mut buffer_data = bincode::serialize(&state).unwrap();
        buffer_data.extend_from_slice(&[0; 16]);
        let rent = LAMPORTS_PER_SOL;
        let mut buffer_account = AccountSharedData::new(rent, buffer_data.len(), &loader::ID);
        buffer_account.set_data_from_slice(&buffer_data);
        self.trident.set_account_custom(&buffer, &buffer_account);
        self.trident.airdrop(&spill, LAMPORTS_PER_SOL);
        let reclaim = Instruction {
            program_id: ID,
            accounts: accounts::ReclaimBuffer {
                governance_authority: addresses.governance,
                config: addresses.config,
                proposal,
                buffer,
                spill,
                loader_program: loader::ID,
            }
            .to_account_metas(None),
            data: instruction::ReclaimBuffer {}.data(),
        };
        let is_reclaimable = status == ProposalStatus::Cancelled
            || (matches!(status, ProposalStatus::Pending | ProposalStatus::Ready)
                && expires_offset < 0);
        let result = self
            .trident
            .process_transaction(&[reclaim], Some("reclaim_buffer"));
        assert_eq!(
            result.is_success(),
            is_reclaimable,
            "unexpected result for {status:?} at {expires_offset}"
        );
        let after = self.trident.get_account(&proposal);
        let stored = UpgradeProposal::try_deserialize(&mut after.data().as_ref()).unwrap();
        assert_eq!(
            stored.status,
            if is_reclaimable {
                ProposalStatus::Reclaimed
            } else {
                status
            }
        );
        assert_eq!(
            self.trident.get_account(&spill).lamports(),
            LAMPORTS_PER_SOL + if is_reclaimable { rent } else { 0 }
        );
        assert_eq!(
            self.trident.get_account(&buffer).lamports(),
            if is_reclaimable { 0 } else { rent }
        );
    }

    fn validate_authority_transfer(&mut self) {
        let addresses = &self.fuzz_accounts;
        let emergency = Pubkey::new_from_array([62; 32]);
        let unrelated = Pubkey::new_from_array([63; 32]);
        let recipient = Pubkey::new_from_array([64; 32]);
        let authority =
            [addresses.governance, emergency, unrelated][self.trident.random_from_range(0..3)];
        let requested_signature = self.trident.random_from_range(0..4) != 0;
        let ready = self.trident.random_from_range(-1..=1);
        let generation = self.trident.random_from_range(0..2u64);
        let role = if self.trident.random_from_range(0..3) == 0 {
            AuthorityRole::Governance
        } else {
            AuthorityRole::ProgramUpgrade
        };
        for signer in [authority, recipient] {
            self.trident.airdrop(&signer, LAMPORTS_PER_SOL);
        }
        let (pending, _) =
            Pubkey::find_program_address(&[b"authority", addresses.config.as_ref()], &ID);
        let request = PendingAuthority {
            role,
            candidate: recipient,
            generation_nonce: generation << 32,
            ready_at: self.trident.get_current_timestamp() + ready,
            refund: addresses.governance,
        };
        let mut request_data = Vec::new();
        request.try_serialize(&mut request_data).unwrap();
        let mut request_account = AccountSharedData::new(LAMPORTS_PER_SOL, request_data.len(), &ID);
        request_account.set_data_from_slice(&request_data);
        self.trident.set_account_custom(&pending, &request_account);
        let program_data = loader::get_program_data_address(&addresses.target);
        let program_state = loader::UpgradeableLoaderState::ProgramData {
            slot: 0,
            upgrade_authority_address: Some(addresses.config),
        };
        let data = bincode::serialize(&program_state).unwrap();
        let mut program_account = AccountSharedData::new(LAMPORTS_PER_SOL, data.len(), &loader::ID);
        program_account.set_data_from_slice(&data);
        self.trident
            .set_account_custom(&program_data, &program_account);
        let mut metas = accounts::TransferProgramAuthority {
            authority,
            config: addresses.config,
            candidate: recipient,
            program_data,
            loader_program: loader::ID,
            pending: Some(pending),
        }
        .to_account_metas(None);
        // Denial-only campaign. Trident 0.12 cannot execute this checked loader CPI.
        let eligible = authority == emergency
            || (authority == addresses.governance
                && role == AuthorityRole::ProgramUpgrade
                && generation == 0
                && ready <= 0);
        metas[2].is_signer = requested_signature && !eligible;
        let transfer = Instruction {
            program_id: ID,
            accounts: metas,
            data: instruction::TransferProgramAuthority {}.data(),
        };
        let result = self
            .trident
            .process_transaction(&[transfer], Some("transfer_program_authority"));
        let unsupported = format!("Program {} not supported by inner instructions", loader::ID);
        assert!(
            result.is_error() && !result.logs().contains(&unsupported),
            "{:?} {}",
            result.get_result(),
            result.logs()
        );
        let observed = self.trident.get_account(&program_data);
        let state: loader::UpgradeableLoaderState = bincode::deserialize(observed.data()).unwrap();
        assert_eq!(state, program_state);
        let config = self.trident.get_account(&addresses.config);
        let state = GatekeeperConfig::try_deserialize(&mut config.data().as_ref()).unwrap();
        assert_eq!(state.proposal_nonce, 0);
    }

    fn validate_upgrade_execution_guards(&mut self) {
        let addresses = &self.fuzz_accounts;
        let buffer = Pubkey::new_from_array([63; 32]);
        let spill = Pubkey::new_from_array([64; 32]);
        let program_data = loader::get_program_data_address(&addresses.target);
        let guard = self.trident.random_from_range(0..5);
        let baseline = 0u64;
        let nonce = if guard == 0 { 1u64 << 32 } else { 0 };
        let (proposal, bump) = Pubkey::find_program_address(
            &[b"proposal", addresses.target.as_ref(), &nonce.to_le_bytes()],
            &ID,
        );
        let now = self.trident.get_current_timestamp();
        let jitter = self.trident.random_from_range(1..=20);
        let state = UpgradeProposal {
            bump,
            nonce,
            target_program: addresses.target,
            baseline_deploy_slot: if guard == 4 {
                self.trident.random_from_range(1..64u64)
            } else {
                baseline
            },
            buffer_account: buffer,
            spill_account: spill,
            expected_elf_sha256: [0; 32],
            git_commit_sha: [0; 20],
            anchor_slot: 70,
            replay_slots: 2,
            diff_root: [1; 32],
            attestation_bitmap: 0b11,
            attestation_count: 2,
            created_at: now - 100,
            expires_at: if guard == 2 {
                now - jitter
            } else {
                now + jitter
            },
            ready_at: if guard == 3 {
                now - 30 + jitter
            } else {
                now - 30 - jitter
            },
            status: if guard == 1 {
                ProposalStatus::Cancelled
            } else {
                ProposalStatus::Ready
            },
        };
        let mut proposal_bytes = Vec::new();
        state.try_serialize(&mut proposal_bytes).unwrap();
        let mut proposal_account =
            AccountSharedData::new(LAMPORTS_PER_SOL, proposal_bytes.len(), &ID);
        proposal_account.set_data_from_slice(&proposal_bytes);
        self.trident
            .set_account_custom(&proposal, &proposal_account);
        let loader_accounts = [
            (
                addresses.target,
                loader::UpgradeableLoaderState::Program {
                    programdata_address: program_data,
                },
                true,
            ),
            (
                program_data,
                loader::UpgradeableLoaderState::ProgramData {
                    slot: baseline,
                    upgrade_authority_address: Some(addresses.config),
                },
                false,
            ),
            (
                buffer,
                loader::UpgradeableLoaderState::Buffer {
                    authority_address: Some(addresses.config),
                },
                false,
            ),
        ];
        for (key, state, executable) in loader_accounts {
            let mut data = bincode::serialize(&state).unwrap();
            if key == program_data {
                static BASELINE: std::sync::OnceLock<Vec<u8>> = std::sync::OnceLock::new();
                let baseline_elf = BASELINE.get_or_init(|| {
                    std::fs::read(
                        "../target/mvp/baseline/sbpf-solana-solana/release/canary_counter.so",
                    )
                    .expect("build the baseline counter with scripts/mvp.py")
                });
                data.extend_from_slice(baseline_elf);
            }
            let mut account = AccountSharedData::new(LAMPORTS_PER_SOL, data.len(), &loader::ID);
            account.set_data_from_slice(&data);
            account.set_executable(executable);
            self.trident.set_account_custom(&key, &account);
        }
        self.trident.airdrop(&spill, LAMPORTS_PER_SOL);
        let execute = Instruction {
            program_id: ID,
            accounts: accounts::ExecuteGatedUpgrade {
                config: addresses.config,
                proposal,
                target_program: addresses.target,
                program_data,
                buffer,
                spill,
                rent: anchor_lang::solana_program::sysvar::rent::ID,
                clock: anchor_lang::solana_program::sysvar::clock::ID,
                loader_program: loader::ID,
            }
            .to_account_metas(None),
            data: instruction::ExecuteGatedUpgrade {}.data(),
        };
        let result = self
            .trident
            .process_transaction(&[execute], Some("execute_gated_upgrade"));
        let expected = [
            "PolicyChanged",
            "WrongStatus",
            "Expired",
            "Timelocked",
            "BaselineChanged",
        ][guard];
        assert!(
            result.is_error() && result.logs().contains(expected),
            "{expected}: {:?} {}",
            result.get_result(),
            result.logs()
        );
        assert_eq!(self.trident.get_account(&proposal).data(), proposal_bytes);
        assert_eq!(
            self.trident.get_account(&buffer).lamports(),
            LAMPORTS_PER_SOL
        );
        assert_eq!(
            self.trident.get_account(&spill).lamports(),
            LAMPORTS_PER_SOL
        );
    }
}

enum Scenario {
    Policy,
    Reclaim,
    ReclaimOrphan,
    Transfer,
    Execute,
}

fn scenario() -> &'static Scenario {
    static SCENARIO: std::sync::OnceLock<Scenario> = std::sync::OnceLock::new();
    SCENARIO.get_or_init(|| match std::env::var("CANARY_FUZZ_SCENARIO").as_deref() {
        Err(_) | Ok("propose_config_update") => Scenario::Policy,
        Ok("reclaim_buffer") => Scenario::Reclaim,
        Ok("reclaim_orphan_buffer") => Scenario::ReclaimOrphan,
        Ok("transfer_program_authority_guards") => Scenario::Transfer,
        Ok("execute_gated_upgrade_guards") => Scenario::Execute,
        Ok(other) => panic!("unknown fuzz scenario: {other}"),
    })
}

fn main() {
    let iterations = std::env::var("CANARY_FUZZ_ITERATIONS")
        .ok()
        .map(|value| value.parse().expect("invalid iteration count"))
        .unwrap_or(1_000_000);
    FuzzTest::fuzz(iterations, 1);
}
