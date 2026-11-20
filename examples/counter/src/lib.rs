use solana_account_info::AccountInfo;
use solana_program_entrypoint::{entrypoint, ProgramResult};
use solana_program_error::ProgramError;
use solana_pubkey::Pubkey;

entrypoint!(process);

fn process(program: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let counter = accounts.first().ok_or(ProgramError::NotEnoughAccountKeys)?;
    // When loaded at the router address, forward to the supplied counter program.
    if let Some(target) = accounts.get(1) {
        let ix = solana_instruction::Instruction::new_with_bytes(
            *target.key,
            data,
            vec![solana_instruction::AccountMeta::new(*counter.key, false)],
        );
        return solana_cpi::invoke(&ix, &[counter.clone(), target.clone()]);
    }
    if counter.owner != program || !counter.is_writable {
        return Err(ProgramError::InvalidAccountData);
    }
    let increment = u64::from_le_bytes(
        data.try_into()
            .map_err(|_| ProgramError::InvalidInstructionData)?,
    );
    // Zero exercises an intentional v2 state change in the replay-mode fixtures.
    #[cfg(feature = "candidate")]
    let increment = if increment == 0 {
        solana_msg::msg!("minimum increment");
        1
    } else {
        increment
    };
    // A synthetic regression that leaves early transactions unchanged.
    #[cfg(feature = "regression-fixture")]
    let increment = if increment == 901 { increment + 1 } else { increment };
    let mut state = counter.try_borrow_mut_data()?;
    let value = u64::from_le_bytes(
        state
            .as_ref()
            .try_into()
            .map_err(|_| ProgramError::InvalidAccountData)?,
    );
    state.copy_from_slice(
        &value
            .checked_add(increment)
            .ok_or(ProgramError::ArithmeticOverflow)?
            .to_le_bytes(),
    );
    #[cfg(feature = "candidate")]
    solana_msg::msg!("counter v2");
    #[cfg(not(feature = "candidate"))]
    solana_msg::msg!("counter v1");
    Ok(())
}
