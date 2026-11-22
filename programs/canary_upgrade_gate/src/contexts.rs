use crate::state::*;
use anchor_lang::prelude::*;
use anchor_lang::solana_program::{bpf_loader_upgradeable as loader, sysvar::instructions};

#[derive(Accounts)]
pub struct InitializeGatekeeper<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    pub governance_authority: Signer<'info>,
    /// CHECK: executable loader-owned program; the canonical ProgramData below binds its authority.
    #[account(executable, owner = loader::ID, constraint = target_program.key() != crate::ID)]
    pub target_program: UncheckedAccount<'info>,
    #[account(address = loader::get_program_data_address(&target_program.key()),
        constraint = program_data.upgrade_authority_address == Some(governance_authority.key()) @ GateError::WrongAuthority)]
    pub program_data: Account<'info, ProgramData>,
    #[account(init, payer = payer, space = 8 + GatekeeperConfig::INIT_SPACE,
        seeds = [b"gatekeeper_cfg", target_program.key().as_ref()], bump)]
    pub config: Account<'info, GatekeeperConfig>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ProposeBufferUpgrade<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    pub governance_authority: Signer<'info>,
    #[account(mut, has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(address = loader::get_program_data_address(&config.target_program),
        constraint = program_data.upgrade_authority_address == Some(config.key()) @ GateError::WrongAuthority)]
    pub program_data: Account<'info, ProgramData>,
    /// CHECK: loader ownership here; Buffer variant and PDA authority checked in the handler.
    #[account(owner = loader::ID)]
    pub buffer: UncheckedAccount<'info>,
    #[account(init, payer = payer, space = 8 + UpgradeProposal::INIT_SPACE,
        seeds = [b"proposal", config.target_program.as_ref(), &config.proposal_nonce.to_le_bytes()[..]], bump)]
    pub proposal: Account<'info, UpgradeProposal>,
    #[account(init, payer = payer, space = 8, seeds = [b"buffer", config.key().as_ref(), buffer.key().as_ref()], bump)]
    pub reservation: Account<'info, BufferReservation>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SubmitCanaryAttestation<'info> {
    #[account(seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, seeds = [b"proposal", config.target_program.as_ref(), &proposal.nonce.to_le_bytes()[..]], bump = proposal.bump)]
    pub proposal: Account<'info, UpgradeProposal>,
    /// CHECK: fixed runtime instructions sysvar, read with checked accessors.
    #[account(address = instructions::ID)]
    pub instruction_sysvar: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct ExecuteGatedUpgrade<'info> {
    #[account(seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, seeds = [b"proposal", config.target_program.as_ref(), &proposal.nonce.to_le_bytes()[..]], bump = proposal.bump)]
    pub proposal: Account<'info, UpgradeProposal>,
    /// CHECK: bound to config and validated by the loader during CPI.
    #[account(mut, executable, owner = loader::ID, address = config.target_program)]
    pub target_program: UncheckedAccount<'info>,
    #[account(mut,
        constraint = program_data.upgrade_authority_address == Some(config.key()) @ GateError::WrongAuthority)]
    pub program_data: Account<'info, ProgramData>,
    /// CHECK: proposal binds address; loader owner, Buffer variant and authority are rechecked.
    #[account(mut, address = proposal.buffer_account, owner = loader::ID)]
    pub buffer: UncheckedAccount<'info>,
    /// CHECK: rent recipient chosen by governance when the proposal was created.
    #[account(mut, address = proposal.spill_account)]
    pub spill: UncheckedAccount<'info>,
    pub rent: Sysvar<'info, Rent>,
    pub clock: Sysvar<'info, Clock>,
    /// CHECK: fixed executable loader program.
    #[account(executable, address = loader::ID)]
    pub loader_program: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct CancelProposal<'info> {
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, seeds = [b"proposal", config.target_program.as_ref(), &proposal.nonce.to_le_bytes()[..]], bump = proposal.bump)]
    pub proposal: Account<'info, UpgradeProposal>,
}

#[derive(Accounts)]
pub struct QueuePolicy<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(init, payer = payer, space = 8 + PendingPolicy::INIT_SPACE, seeds = [b"policy", config.key().as_ref()], bump)]
    pub pending: Account<'info, PendingPolicy>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ResolvePolicy<'info> {
    pub governance_authority: Signer<'info>,
    #[account(mut, has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, close = refund, has_one = refund, seeds = [b"policy", config.key().as_ref()], bump)]
    pub pending: Account<'info, PendingPolicy>,
    /// CHECK: original rent payer stored in the pending update.
    #[account(mut)]
    pub refund: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct CloseProposal<'info> {
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, close = spill, seeds = [b"proposal", config.target_program.as_ref(), &proposal.nonce.to_le_bytes()[..]], bump = proposal.bump)]
    pub proposal: Account<'info, UpgradeProposal>,
    #[account(mut, close = spill, seeds = [b"buffer", config.key().as_ref(), proposal.buffer_account.as_ref()], bump)]
    pub reservation: Account<'info, BufferReservation>,
    /// CHECK: fixed rent recipient.
    #[account(mut, address = proposal.spill_account)]
    pub spill: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct EmergencyUpgrade<'info> {
    pub emergency_authority: Signer<'info>,
    #[account(has_one = emergency_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    /// CHECK: config binds the loader-owned target.
    #[account(mut, executable, owner = loader::ID, address = config.target_program)]
    pub target_program: UncheckedAccount<'info>,
    #[account(mut,
        constraint = program_data.upgrade_authority_address == Some(config.key()) @ GateError::WrongAuthority)]
    pub program_data: Account<'info, ProgramData>,
    /// CHECK: Buffer tag and authority are checked before CPI.
    #[account(mut, owner = loader::ID)]
    pub buffer: UncheckedAccount<'info>,
    /// CHECK: recipient selected by the signing emergency authority.
    #[account(mut)]
    pub spill: UncheckedAccount<'info>,
    pub rent: Sysvar<'info, Rent>,
    pub clock: Sysvar<'info, Clock>,
    /// CHECK: native executable loader.
    #[account(executable, address = loader::ID)]
    pub loader_program: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct ReclaimBuffer<'info> {
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, seeds = [b"proposal", config.target_program.as_ref(), &proposal.nonce.to_le_bytes()[..]], bump = proposal.bump)]
    pub proposal: Account<'info, UpgradeProposal>,
    /// CHECK: proposal binds address and handler verifies live loader state.
    #[account(mut, address = proposal.buffer_account)]
    pub buffer: UncheckedAccount<'info>,
    /// CHECK: fixed at proposal creation.
    #[account(mut, address = proposal.spill_account)]
    pub spill: UncheckedAccount<'info>,
    /// CHECK: native loader.
    #[account(executable, address = loader::ID)]
    pub loader_program: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct ReclaimOrphanBuffer<'info> {
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    /// CHECK: loader ownership here; Buffer variant and gate authority checked in the handler.
    #[account(mut, owner = loader::ID)]
    pub buffer: UncheckedAccount<'info>,
    /// CHECK: the buffer's reservation PDA, which must not exist for an orphan.
    #[account(seeds = [b"buffer", config.key().as_ref(), buffer.key().as_ref()], bump)]
    pub reservation: UncheckedAccount<'info>,
    /// CHECK: rent recipient chosen by governance.
    #[account(mut)]
    pub spill: UncheckedAccount<'info>,
    /// CHECK: native loader.
    #[account(executable, address = loader::ID)]
    pub loader_program: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct QueueAuthority<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    pub governance_authority: Signer<'info>,
    #[account(has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(init, payer = payer, space = 8 + PendingAuthority::INIT_SPACE, seeds = [b"authority", config.key().as_ref()], bump)]
    pub pending: Account<'info, PendingAuthority>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ResolveAuthority<'info> {
    pub governance_authority: Signer<'info>,
    #[account(mut, has_one = governance_authority, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    #[account(mut, close = refund, has_one = refund, seeds = [b"authority", config.key().as_ref()], bump)]
    pub pending: Account<'info, PendingAuthority>,
    /// CHECK: fixed original payer.
    #[account(mut)]
    pub refund: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct RotateAuthority<'info> {
    pub change: ResolveAuthority<'info>,
    pub candidate: Signer<'info>,
}

#[derive(Accounts)]
pub struct TransferProgramAuthority<'info> {
    pub authority: Signer<'info>,
    #[account(mut, seeds = [b"gatekeeper_cfg", config.target_program.as_ref()], bump = config.bump)]
    pub config: Account<'info, GatekeeperConfig>,
    pub candidate: Signer<'info>,
    #[account(mut, address = loader::get_program_data_address(&config.target_program),
        constraint = program_data.upgrade_authority_address == Some(config.key()) @ GateError::WrongAuthority)]
    pub program_data: Account<'info, ProgramData>,
    /// CHECK: native loader.
    #[account(executable, address = loader::ID)]
    pub loader_program: UncheckedAccount<'info>,
    #[account(seeds = [b"authority", config.key().as_ref()], bump)]
    pub pending: Option<Account<'info, PendingAuthority>>,
}
