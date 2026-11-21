use anchor_lang::prelude::*;
use anchor_lang::solana_program::{bpf_loader_upgradeable as loader, program::invoke_signed};
use canary_types::CanaryAttestationPayload;

mod contexts;
mod ed25519_check;
pub mod state;
#[cfg(test)]
mod tests;
pub use contexts::*;
use state::*;

declare_id!("38SJ9rbJgLtibh4bL1VyoGb5uMaEQtSPBJZbTFahYtmz");

#[program]
pub mod canary_upgrade_gate {
    use super::*;

    pub fn initialize_gatekeeper(
        ctx: Context<InitializeGatekeeper>,
        policy: Policy,
        emergency_authority: Pubkey,
    ) -> Result<()> {
        policy.validate()?;
        let governance = ctx.accounts.governance_authority.key();
        require!(
            emergency_authority != governance && emergency_authority != Pubkey::default(),
            GateError::WrongAuthority
        );
        let target_program = ctx.accounts.target_program.key();
        ctx.accounts.config.set_inner(GatekeeperConfig {
            version: 2,
            bump: ctx.bumps.config,
            target_program,
            governance_authority: governance,
            emergency_authority,
            policy,
            proposal_nonce: 0,
        });
        emit!(GatekeeperInitialized {
            target_program,
            config: ctx.accounts.config.key()
        });
        Ok(())
    }

    pub fn propose_buffer_upgrade(
        ctx: Context<ProposeBufferUpgrade>,
        spill: Pubkey,
        expected_elf_sha256: [u8; 32],
        git_commit_sha: [u8; 20],
        anchor_slot: Option<u64>,
        replay_slots: u64,
    ) -> Result<()> {
        let clock = Clock::get()?;
        let anchor_slot = anchor_slot.unwrap_or(clock.slot);
        let config = &mut ctx.accounts.config;
        check_buffer(&ctx.accounts.buffer, config.key())?;
        require!(
            spill != ctx.accounts.proposal.key()
                && spill != ctx.accounts.reservation.key()
                && spill != ctx.accounts.buffer.key()
                && spill != ctx.accounts.program_data.key()
                && spill != config.target_program,
            GateError::WrongLoaderAccount
        );
        require!(
            anchor_slot <= clock.slot
                && clock.slot - anchor_slot <= 32
                && anchor_slot > ctx.accounts.program_data.slot
                && replay_slots >= config.policy.min_replay_slots,
            GateError::InvalidWindow
        );
        anchor_slot
            .checked_add(replay_slots)
            .ok_or(GateError::Overflow)?;
        let nonce = config.next_nonce()?;
        ctx.accounts.proposal.set_inner(UpgradeProposal {
            bump: ctx.bumps.proposal,
            nonce,
            target_program: config.target_program,
            baseline_deploy_slot: ctx.accounts.program_data.slot,
            buffer_account: ctx.accounts.buffer.key(),
            spill_account: spill,
            expected_elf_sha256,
            git_commit_sha,
            anchor_slot,
            replay_slots,
            diff_root: [0; 32],
            attestation_bitmap: 0,
            attestation_count: 0,
            created_at: clock.unix_timestamp,
            expires_at: clock
                .unix_timestamp
                .checked_add(config.policy.proposal_expiry_seconds)
                .ok_or(GateError::Overflow)?,
            ready_at: 0,
            status: ProposalStatus::Pending,
        });
        emit!(ProposalCreated {
            proposal: ctx.accounts.proposal.key(),
            target_program: config.target_program,
            nonce,
            anchor_slot,
            replay_slots
        });
        Ok(())
    }

    /// The vote payload is read from the preceding Ed25519 instruction, so it
    /// is signed and parsed exactly once.
    pub fn submit_verified_attestation(ctx: Context<SubmitCanaryAttestation>) -> Result<()> {
        let ix = ed25519_check::previous_verification(&ctx.accounts.instruction_sysvar)?;
        let (verifier, payload) = ed25519_check::signed_payload(&ix)?;
        accept_vote(ctx, verifier, payload)
    }

    pub fn execute_gated_upgrade(ctx: Context<ExecuteGatedUpgrade>) -> Result<()> {
        let a = ctx.accounts;
        a.config.check_generation(a.proposal.nonce)?;
        a.proposal.check_execution(
            &a.config.policy,
            a.clock.unix_timestamp,
            a.program_data.slot,
        )?;
        run_upgrade(
            &a.config,
            UpgradeAccounts {
                target: &a.target_program,
                program_data: &a.program_data,
                buffer: &a.buffer,
                spill: &a.spill,
                rent: &a.rent,
                clock: &a.clock,
                loader: &a.loader_program,
            },
        )?;
        a.proposal.status = ProposalStatus::Executed;
        emit!(UpgradeExecuted {
            proposal: a.proposal.key(),
            target_program: a.config.target_program
        });
        Ok(())
    }

    pub fn emergency_override_upgrade(ctx: Context<EmergencyUpgrade>) -> Result<()> {
        let a = ctx.accounts;
        run_upgrade(
            &a.config,
            UpgradeAccounts {
                target: &a.target_program,
                program_data: &a.program_data,
                buffer: &a.buffer,
                spill: &a.spill,
                rent: &a.rent,
                clock: &a.clock,
                loader: &a.loader_program,
            },
        )?;
        emit!(EmergencyOverrideTriggered {
            target_program: a.config.target_program,
            buffer: a.buffer.key(),
            authority: a.emergency_authority.key()
        });
        Ok(())
    }

    pub fn propose_config_update(ctx: Context<QueuePolicy>, policy: Policy) -> Result<()> {
        policy.validate()?;
        let ready_at = Clock::get()?
            .unix_timestamp
            .checked_add(ctx.accounts.config.policy.timelock_seconds)
            .ok_or(GateError::Overflow)?;
        ctx.accounts.pending.set_inner(PendingPolicy {
            generation_nonce: ctx.accounts.config.proposal_nonce,
            ready_at,
            refund: ctx.accounts.payer.key(),
            policy,
        });
        emit!(PolicyUpdateQueued {
            config: ctx.accounts.config.key(),
            ready_at
        });
        Ok(())
    }

    pub fn execute_config_update(ctx: Context<ResolvePolicy>) -> Result<()> {
        require!(
            Clock::get()?.unix_timestamp >= ctx.accounts.pending.ready_at,
            GateError::Timelocked
        );
        ctx.accounts
            .config
            .check_generation(ctx.accounts.pending.generation_nonce)?;
        ctx.accounts
            .config
            .rotate(ctx.accounts.pending.policy.clone())?;
        emit!(PolicyUpdated {
            config: ctx.accounts.config.key(),
            next_nonce: ctx.accounts.config.proposal_nonce
        });
        Ok(())
    }

    pub fn cancel_config_update(_ctx: Context<ResolvePolicy>) -> Result<()> {
        Ok(())
    }

    pub fn reclaim_buffer(ctx: Context<ReclaimBuffer>) -> Result<()> {
        let a = ctx.accounts;
        let p = &mut a.proposal;
        if p.status != ProposalStatus::Cancelled {
            require!(
                matches!(p.status, ProposalStatus::Pending | ProposalStatus::Ready),
                GateError::WrongStatus
            );
            require!(
                Clock::get()?.unix_timestamp > p.expires_at,
                GateError::WrongStatus
            );
        }
        // An emergency upgrade can have consumed this buffer already.
        if !(a.buffer.data_is_empty() && a.buffer.owner == &anchor_lang::system_program::ID) {
            require_keys_eq!(*a.buffer.owner, loader::ID, GateError::WrongLoaderAccount);
            check_buffer(&a.buffer, a.config.key())?;
            let ix =
                loader::close_any(&a.buffer.key(), &a.spill.key(), Some(&a.config.key()), None);
            invoke_signed(
                &ix,
                &[
                    a.buffer.to_account_info(),
                    a.spill.to_account_info(),
                    a.config.to_account_info(),
                    a.loader_program.to_account_info(),
                ],
                &[&[
                    b"gatekeeper_cfg",
                    a.config.target_program.as_ref(),
                    &[a.config.bump],
                ]],
            )?;
        }
        p.status = ProposalStatus::Reclaimed;
        Ok(())
    }

    pub fn propose_authority_change(
        ctx: Context<QueueAuthority>,
        role: AuthorityRole,
        candidate: Pubkey,
    ) -> Result<()> {
        let a = ctx.accounts;
        require!(
            candidate != Pubkey::default() && candidate != a.config.key(),
            GateError::WrongAuthority
        );
        if role != AuthorityRole::ProgramUpgrade {
            require!(
                candidate != a.config.governance_authority
                    && candidate != a.config.emergency_authority,
                GateError::WrongAuthority
            );
        }
        let ready_at = Clock::get()?
            .unix_timestamp
            .checked_add(a.config.policy.timelock_seconds)
            .ok_or(GateError::Overflow)?;
        a.pending.set_inner(PendingAuthority {
            role,
            candidate,
            generation_nonce: a.config.proposal_nonce,
            ready_at,
            refund: a.payer.key(),
        });
        Ok(())
    }

    pub fn cancel_authority_change(_ctx: Context<ResolveAuthority>) -> Result<()> {
        Ok(())
    }

    pub fn execute_authority_change(ctx: Context<RotateAuthority>) -> Result<()> {
        let a = &mut ctx.accounts.change;
        check_change(&a.config, &a.pending, ctx.accounts.candidate.key())?;
        match a.pending.role {
            AuthorityRole::Governance => a.config.governance_authority = a.pending.candidate,
            AuthorityRole::Emergency => a.config.emergency_authority = a.pending.candidate,
            AuthorityRole::ProgramUpgrade => return err!(GateError::WrongAuthority),
        }
        a.config.advance_generation()
    }

    pub fn transfer_program_authority(ctx: Context<TransferProgramAuthority>) -> Result<()> {
        let a = ctx.accounts;
        if a.authority.key() != a.config.emergency_authority {
            require_keys_eq!(
                a.authority.key(),
                a.config.governance_authority,
                GateError::WrongAuthority
            );
            let pending = a.pending.as_ref().ok_or(GateError::WrongAuthority)?;
            require!(
                pending.role == AuthorityRole::ProgramUpgrade,
                GateError::WrongAuthority
            );
            check_change(&a.config, pending, a.candidate.key())?;
        }
        let next = a.candidate.key();
        require!(
            next != a.config.key() && next != Pubkey::default(),
            GateError::WrongAuthority
        );
        let ix =
            loader::set_upgrade_authority_checked(&a.config.target_program, &a.config.key(), &next);
        invoke_signed(
            &ix,
            &[
                a.program_data.to_account_info(),
                a.config.to_account_info(),
                a.candidate.to_account_info(),
                a.loader_program.to_account_info(),
            ],
            &[&[
                b"gatekeeper_cfg",
                a.config.target_program.as_ref(),
                &[a.config.bump],
            ]],
        )?;
        // Generation invalidation also consumes the queued exit. Governance can
        // recover its rent with cancel_authority_change.
        a.config.advance_generation()
    }

    pub fn close_proposal(ctx: Context<CloseProposal>) -> Result<()> {
        require!(
            matches!(
                ctx.accounts.proposal.status,
                ProposalStatus::Executed | ProposalStatus::Reclaimed
            ),
            GateError::WrongStatus
        );
        Ok(())
    }

    pub fn cancel_proposal(ctx: Context<CancelProposal>) -> Result<()> {
        let proposal = &mut ctx.accounts.proposal;
        require!(
            matches!(
                proposal.status,
                ProposalStatus::Pending | ProposalStatus::Ready
            ),
            GateError::WrongStatus
        );
        proposal.status = ProposalStatus::Cancelled;
        emit!(ProposalCancelled {
            proposal: proposal.key()
        });
        Ok(())
    }
}

fn check_change(
    config: &GatekeeperConfig,
    change: &PendingAuthority,
    candidate: Pubkey,
) -> Result<()> {
    config.check_generation(change.generation_nonce)?;
    require_keys_eq!(change.candidate, candidate, GateError::WrongAuthority);
    require!(
        Clock::get()?.unix_timestamp >= change.ready_at,
        GateError::Timelocked
    );
    Ok(())
}

fn accept_vote(
    ctx: Context<SubmitCanaryAttestation>,
    verifier: Pubkey,
    payload: CanaryAttestationPayload,
) -> Result<()> {
    let proposal = &mut ctx.accounts.proposal;
    ctx.accounts.config.check_generation(proposal.nonce)?;
    proposal.record_vote(
        &ctx.accounts.config.policy,
        &payload,
        verifier,
        &Clock::get()?,
    )?;
    emit!(AttestationAccepted {
        proposal: proposal.key(),
        verifier,
        count: proposal.attestation_count,
        status: proposal.status
    });
    Ok(())
}

struct UpgradeAccounts<'a, 'info> {
    target: &'a UncheckedAccount<'info>,
    program_data: &'a Account<'info, ProgramData>,
    buffer: &'a UncheckedAccount<'info>,
    spill: &'a UncheckedAccount<'info>,
    rent: &'a Sysvar<'info, Rent>,
    clock: &'a Sysvar<'info, Clock>,
    loader: &'a UncheckedAccount<'info>,
}

fn upgrade_ix(
    target: Pubkey,
    program_data: Pubkey,
    buffer: Pubkey,
    authority: Pubkey,
    spill: Pubkey,
) -> anchor_lang::solana_program::instruction::Instruction {
    use anchor_lang::solana_program::{
        instruction::Instruction, loader_upgradeable_instruction::UpgradeableLoaderInstruction,
        sysvar,
    };
    Instruction::new_with_bincode(
        loader::ID,
        &UpgradeableLoaderInstruction::Upgrade,
        vec![
            AccountMeta::new(program_data, false),
            AccountMeta::new(target, false),
            AccountMeta::new(buffer, false),
            AccountMeta::new(spill, false),
            AccountMeta::new_readonly(sysvar::rent::ID, false),
            AccountMeta::new_readonly(sysvar::clock::ID, false),
            AccountMeta::new_readonly(authority, true),
        ],
    )
}

fn run_upgrade<'info>(
    config: &Account<'info, GatekeeperConfig>,
    a: UpgradeAccounts<'_, 'info>,
) -> Result<()> {
    check_buffer(a.buffer, config.key())?;
    require!(
        a.spill.key() != a.buffer.key()
            && a.spill.key() != a.program_data.key()
            && a.spill.key() != a.target.key(),
        GateError::WrongLoaderAccount
    );
    // The loader owns this executable Program record. Its ProgramData pointer
    // binds the account directly, avoiding two variable-cost PDA searches.
    {
        let data = a.target.try_borrow_data()?;
        let program = UpgradeableLoaderState::try_deserialize(&mut &data[..])?;
        require!(
            matches!(program, UpgradeableLoaderState::Program { programdata_address } if programdata_address == a.program_data.key()),
            GateError::WrongLoaderAccount
        );
    }
    let ix = upgrade_ix(
        config.target_program,
        a.program_data.key(),
        a.buffer.key(),
        config.key(),
        a.spill.key(),
    );
    let seeds: &[&[u8]] = &[
        b"gatekeeper_cfg",
        config.target_program.as_ref(),
        &[config.bump],
    ];
    invoke_signed(
        &ix,
        &[
            a.program_data.to_account_info(),
            a.target.to_account_info(),
            a.buffer.to_account_info(),
            a.spill.to_account_info(),
            a.rent.to_account_info(),
            a.clock.to_account_info(),
            config.to_account_info(),
            a.loader.to_account_info(),
        ],
        &[seeds],
    )?;
    Ok(())
}

#[event]
pub struct EmergencyOverrideTriggered {
    pub target_program: Pubkey,
    pub buffer: Pubkey,
    pub authority: Pubkey,
}
#[event]
pub struct PolicyUpdateQueued {
    pub config: Pubkey,
    pub ready_at: i64,
}
#[event]
pub struct PolicyUpdated {
    pub config: Pubkey,
    pub next_nonce: u64,
}

fn check_buffer(buffer: &AccountInfo, authority: Pubkey) -> Result<()> {
    let data = buffer.try_borrow_data()?;
    require!(
        data.len() > UpgradeableLoaderState::size_of_buffer_metadata(),
        GateError::WrongLoaderAccount
    );
    let state = UpgradeableLoaderState::try_deserialize(&mut &data[..])?;
    require!(
        matches!(state, UpgradeableLoaderState::Buffer { authority_address: Some(key) } if key == authority),
        GateError::WrongLoaderAccount
    );
    Ok(())
}

#[event]
pub struct GatekeeperInitialized {
    pub target_program: Pubkey,
    pub config: Pubkey,
}
#[event]
pub struct ProposalCreated {
    pub proposal: Pubkey,
    pub target_program: Pubkey,
    pub nonce: u64,
    pub anchor_slot: u64,
    pub replay_slots: u64,
}
#[event]
pub struct AttestationAccepted {
    pub proposal: Pubkey,
    pub verifier: Pubkey,
    pub count: u8,
    pub status: ProposalStatus,
}
#[event]
pub struct UpgradeExecuted {
    pub proposal: Pubkey,
    pub target_program: Pubkey,
}
#[event]
pub struct ProposalCancelled {
    pub proposal: Pubkey,
}
