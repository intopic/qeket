use anchor_lang::prelude::*;

pub const DOMAIN: [u8; 16] = *b"CANARY_GATE_M1__";

/// Fixed-width Borsh message shared by the verifier and the gate.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq)]
pub struct CanaryAttestationPayload {
    pub domain_separator: [u8; 16],
    pub target_program: Pubkey,
    pub proposal_nonce: u64,
    pub buffer_account: Pubkey,
    pub anchor_slot: u64,
    pub replay_slots: u64,
    pub elf_sha256: [u8; 32],
    pub diff_root: [u8; 32],
    pub replayed_tx_count: u32,
    pub failed_tx_count: u32,
    pub max_cu_delta_bps: u16,
    pub expires_at_slot: u64,
}
