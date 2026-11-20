use anchor_lang::{
    solana_program::bpf_loader_upgradeable as loader, AccountDeserialize, AccountSerialize,
    InstructionData, ToAccountMetas,
};
use canary_upgrade_gate::{
    accounts, instruction,
    state::{GatekeeperConfig, Policy, ProposalStatus, ReplayMode, UpgradeProposal},
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
        if reclaim_mode() {
            self.validate_buffer_reclaim();
        } else {
            self.validate_policy_updates();
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
        let valid = (2..=3).contains(&next.verifier_count)
            && (2..=next.verifier_count).contains(&next.threshold)
            && next.min_replay_txs > 0
            && next.min_replay_slots > 0
            && next.timelock_seconds > 0
            && next.proposal_expiry_seconds > next.timelock_seconds
            && next.verifiers[0] != next.verifiers[1];
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
}

fn reclaim_mode() -> bool {
    static RECLAIM: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *RECLAIM
        .get_or_init(|| std::env::var("CANARY_FUZZ_SCENARIO").as_deref() == Ok("reclaim_buffer"))
}

fn main() {
    let iterations = std::env::var("CANARY_FUZZ_ITERATIONS")
        .ok()
        .map(|value| value.parse().expect("invalid iteration count"))
        .unwrap_or(1_000_000);
    FuzzTest::fuzz(iterations, 1);
}
