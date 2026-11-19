use anchor_lang::prelude::*;
use canary_types::{CanaryAttestationPayload, DOMAIN};

#[derive(AnchorSerialize, AnchorDeserialize, Clone, InitSpace)]
pub struct Policy {
    pub replay_mode: ReplayMode,
    pub verifiers: [Pubkey; 7],
    pub verifier_count: u8,
    pub threshold: u8,
    pub max_cu_delta_bps: u16,
    pub min_replay_txs: u32,
    pub min_replay_slots: u64,
    pub timelock_seconds: i64,
    pub proposal_expiry_seconds: i64,
}

#[derive(
    AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, Default, PartialEq, Eq, InitSpace,
)]
pub enum ReplayMode {
    #[default]
    StrictEquality,
    NoRevertAndCuBound,
}

impl ReplayMode {
    pub fn domain(self) -> [u8; 16] {
        match self {
            Self::StrictEquality => DOMAIN,
            Self::NoRevertAndCuBound => *b"CANARY_GATE_NR1_",
        }
    }
}

impl Policy {
    pub fn validate(&self) -> Result<()> {
        let n = usize::from(self.verifier_count);
        require!(
            n <= self.verifiers.len()
                && self.threshold >= 2
                && self.threshold <= self.verifier_count
                && self.min_replay_txs > 0
                && self.min_replay_slots > 0
                && self.timelock_seconds > 0
                && self.proposal_expiry_seconds > self.timelock_seconds,
            GateError::InvalidPolicy
        );
        for (index, key) in self.verifiers[..n].iter().enumerate() {
            require!(
                *key != Pubkey::default() && !self.verifiers[..index].contains(key),
                GateError::InvalidPolicy
            );
        }
        Ok(())
    }
}

#[account]
#[derive(InitSpace)]
pub struct GatekeeperConfig {
    pub version: u8,
    pub bump: u8,
    pub target_program: Pubkey,
    pub governance_authority: Pubkey,
    pub emergency_authority: Pubkey,
    pub policy: Policy,
    pub proposal_nonce: u64,
}

// The upper 32 bits identify the policy generation. The lower bits number
// proposals within it. Rotation invalidates votes without scanning accounts.
impl GatekeeperConfig {
    pub fn next_nonce(&mut self) -> Result<u64> {
        let current = self.proposal_nonce;
        require!(current as u32 != u32::MAX, GateError::Overflow);
        self.proposal_nonce += 1;
        Ok(current)
    }

    pub fn check_generation(&self, nonce: u64) -> Result<()> {
        require!(
            nonce >> 32 == self.proposal_nonce >> 32,
            GateError::PolicyChanged
        );
        Ok(())
    }

    pub fn advance_generation(&mut self) -> Result<()> {
        let generation = ((self.proposal_nonce >> 32) as u32)
            .checked_add(1)
            .ok_or(GateError::Overflow)?;
        self.proposal_nonce = u64::from(generation) << 32;
        Ok(())
    }

    pub fn rotate(&mut self, policy: Policy) -> Result<()> {
        self.advance_generation()?;
        self.policy = policy;
        Ok(())
    }
}

#[account]
#[derive(InitSpace)]
pub struct PendingPolicy {
    pub generation_nonce: u64,
    pub ready_at: i64,
    pub refund: Pubkey,
    pub policy: Policy,
}

#[derive(AnchorSerialize, AnchorDeserialize, Copy, Clone, Debug, PartialEq, Eq, InitSpace)]
pub enum ProposalStatus {
    Pending,
    Ready,
    Executed,
    Cancelled,
    Reclaimed,
}

#[account]
#[derive(InitSpace)]
pub struct UpgradeProposal {
    pub bump: u8,
    pub nonce: u64,
    pub target_program: Pubkey,
    pub baseline_deploy_slot: u64,
    pub buffer_account: Pubkey,
    pub spill_account: Pubkey,
    pub expected_elf_sha256: [u8; 32],
    pub git_commit_sha: [u8; 20],
    pub anchor_slot: u64,
    pub replay_slots: u64,
    pub diff_root: [u8; 32],
    pub attestation_bitmap: u8,
    pub attestation_count: u8,
    pub created_at: i64,
    pub expires_at: i64,
    pub ready_at: i64,
    pub status: ProposalStatus,
}

impl UpgradeProposal {
    pub fn window_end(&self) -> Result<u64> {
        self.anchor_slot
            .checked_add(self.replay_slots)
            .ok_or_else(|| error!(GateError::Overflow))
    }

    fn check_expiry(&self, now: i64) -> Result<()> {
        require!(now <= self.expires_at, GateError::Expired);
        Ok(())
    }

    pub fn record_vote(
        &mut self,
        policy: &Policy,
        payload: &CanaryAttestationPayload,
        verifier: Pubkey,
        clock: &Clock,
    ) -> Result<()> {
        require!(
            self.status == ProposalStatus::Pending,
            GateError::WrongStatus
        );
        self.check_expiry(clock.unix_timestamp)?;
        // The end slot is inclusive. Wait for it to finish before accepting evidence.
        require!(
            clock.slot > self.window_end()? && clock.slot <= payload.expires_at_slot,
            GateError::InvalidWindow
        );
        require!(
            payload.domain_separator == policy.replay_mode.domain()
                && payload.target_program == self.target_program
                && payload.proposal_nonce == self.nonce
                && payload.buffer_account == self.buffer_account
                && payload.anchor_slot == self.anchor_slot
                && payload.replay_slots == self.replay_slots
                && payload.elf_sha256 == self.expected_elf_sha256,
            GateError::WrongPayload
        );
        require!(
            payload.failed_tx_count == 0
                && payload.replayed_tx_count >= policy.min_replay_txs
                && payload.max_cu_delta_bps <= policy.max_cu_delta_bps,
            GateError::ReplayPolicy
        );
        let index = policy.verifiers[..usize::from(policy.verifier_count)]
            .iter()
            .position(|key| key == &verifier)
            .ok_or(GateError::UnknownVerifier)?;
        let bit = 1u8 << index;
        require!(self.attestation_bitmap & bit == 0, GateError::AlreadyVoted);
        require!(
            self.attestation_count == 0 || self.diff_root == payload.diff_root,
            GateError::RootMismatch
        );
        self.diff_root = payload.diff_root;
        self.attestation_bitmap |= bit;
        self.attestation_count = self.attestation_bitmap.count_ones() as u8;
        if self.attestation_count == policy.threshold {
            self.status = ProposalStatus::Ready;
            self.ready_at = clock.unix_timestamp;
        }
        Ok(())
    }

    pub fn check_execution(&self, policy: &Policy, now: i64, baseline: u64) -> Result<()> {
        require!(self.status == ProposalStatus::Ready, GateError::WrongStatus);
        self.check_expiry(now)?;
        let earliest = self
            .ready_at
            .checked_add(policy.timelock_seconds)
            .ok_or(GateError::Overflow)?;
        require!(now >= earliest, GateError::Timelocked);
        require!(
            baseline == self.baseline_deploy_slot,
            GateError::BaselineChanged
        );
        Ok(())
    }
}

#[error_code]
pub enum GateError {
    #[msg("Invalid verifier set, replay limits or timing policy")]
    InvalidPolicy,
    #[msg("Incorrect authority")]
    WrongAuthority,
    #[msg("Unexpected loader account state or address")]
    WrongLoaderAccount,
    #[msg("Window is incomplete, expired or outside the permitted range")]
    InvalidWindow,
    #[msg("Proposal has expired")]
    Expired,
    #[msg("Checked arithmetic overflow")]
    Overflow,
    #[msg("Invalid proposal state for this operation")]
    WrongStatus,
    #[msg("Signed message does not identify this proposal")]
    WrongPayload,
    #[msg("Replay evidence does not satisfy policy")]
    ReplayPolicy,
    #[msg("Signer is outside the active verifier set")]
    UnknownVerifier,
    #[msg("Verifier already voted")]
    AlreadyVoted,
    #[msg("Verifiers disagree on the state difference root")]
    RootMismatch,
    #[msg("Expected an adjacent canonical Ed25519 verification instruction")]
    InvalidSignatureInstruction,
    #[msg("Timelock has not elapsed")]
    Timelocked,
    #[msg("Target changed since proposal creation")]
    BaselineChanged,
    #[msg("Proposal belongs to a previous policy generation")]
    PolicyChanged,
}

#[account]
#[derive(InitSpace)]
pub struct BufferReservation {}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, InitSpace)]
pub enum AuthorityRole {
    Governance,
    Emergency,
    ProgramUpgrade,
}

#[account]
#[derive(InitSpace)]
pub struct PendingAuthority {
    pub role: AuthorityRole,
    pub candidate: Pubkey,
    pub generation_nonce: u64,
    pub ready_at: i64,
    pub refund: Pubkey,
}
