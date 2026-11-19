use solana_account_info::AccountInfo;
use solana_program_entrypoint::{entrypoint, ProgramResult};
use solana_program_error::ProgramError;
use solana_pubkey::Pubkey;

entrypoint!(process);

fn process(program: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let counter = accounts.first().ok_or(ProgramError::NotEnoughAccountKeys)?;
    if counter.owner != program || !counter.is_writable {
        return Err(ProgramError::InvalidAccountData);
    }
    let increment = u64::from_le_bytes(
        data.try_into()
            .map_err(|_| ProgramError::InvalidInstructionData)?,
    );
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
