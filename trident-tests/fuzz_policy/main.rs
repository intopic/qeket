use anchor_lang::{AccountSerialize, InstructionData, ToAccountMetas};
use canary_upgrade_gate::{
    accounts, instruction,
    state::{GatekeeperConfig, Policy, ReplayMode},
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
}

fn main() {
    let iterations = std::env::var("CANARY_FUZZ_ITERATIONS")
        .ok()
        .map(|value| value.parse().expect("invalid iteration count"))
        .unwrap_or(1_000_000);
    FuzzTest::fuzz(iterations, 1);
}
