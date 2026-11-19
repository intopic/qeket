use crate::state::GateError;
use anchor_lang::prelude::*;
use anchor_lang::solana_program::{
    ed25519_program,
    instruction::Instruction,
    sysvar::instructions::{load_current_index_checked, load_instruction_at_checked},
};

pub fn previous_verification(sysvar: &AccountInfo) -> Result<Instruction> {
    let current = usize::from(load_current_index_checked(sysvar)?);
    let previous = current
        .checked_sub(1)
        .ok_or(GateError::InvalidSignatureInstruction)?;
    Ok(load_instruction_at_checked(previous, sysvar)?)
}

pub fn signed_payload(
    ix: &Instruction,
) -> Result<(Pubkey, canary_types::CanaryAttestationPayload)> {
    let bytes = ix
        .data
        .get(112..)
        .ok_or(GateError::InvalidSignatureInstruction)?;
    let key = signer_from_instruction(ix, bytes)?;
    let payload = canary_types::CanaryAttestationPayload::try_from_slice(bytes)
        .map_err(|_| GateError::WrongPayload)?;
    Ok((key, payload))
}

// The runtime verifies the signature. Bind that result to the exact bytes consumed here.
pub(crate) fn signer_from_instruction(ix: &Instruction, message: &[u8]) -> Result<Pubkey> {
    let data = &ix.data;
    require!(
        ix.program_id == ed25519_program::ID
            && ix.accounts.is_empty()
            && data.len() >= 112
            && data.len() - 112 == message.len(),
        GateError::InvalidSignatureInstruction
    );
    let msg_len =
        u16::try_from(message.len()).map_err(|_| GateError::InvalidSignatureInstruction)?;
    let offsets = [48, u16::MAX, 16, u16::MAX, 112, msg_len, u16::MAX];
    require!(data[..2] == [1, 0], GateError::InvalidSignatureInstruction);
    for (field, expected) in data[2..16].chunks_exact(2).zip(offsets) {
        require!(
            field == expected.to_le_bytes(),
            GateError::InvalidSignatureInstruction
        );
    }
    require!(&data[112..] == message, GateError::WrongPayload);
    let key: [u8; 32] = data[16..48]
        .try_into()
        .map_err(|_| GateError::InvalidSignatureInstruction)?;
    Ok(Pubkey::new_from_array(key))
}
