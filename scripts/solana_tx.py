"""Dependency-free Ed25519 signing and legacy Solana transactions for the worker vote."""
import hashlib
import json
import os
import struct
import sys

from devnet import from_base58, to_base58

ED25519_PROGRAM = "Ed25519SigVerify111111111111111111111111111"
INSTRUCTIONS_SYSVAR = "Sysvar1nstructions1111111111111111111111111"
DOMAINS = {"StrictEquality": b"CANARY_GATE_M1__", "NoRevertAndCuBound": b"CANARY_GATE_NR1_"}

# RFC 8032 Ed25519 over GF(2^255 - 19), extended homogeneous coordinates.
P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    e = (y1 - x1) * (y2 - x2) % P
    f = (y1 + x1) * (y2 + x2) % P
    g = 2 * t1 * t2 * D % P
    h = 2 * z1 * z2 % P
    e, f, g, h = f - e, h - g, h + g, f + e
    return e * f % P, g * h % P, f * g % P, e * h % P


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode(point):
    x, y, z, _ = point
    inverse = pow(z, P - 2, P)
    x, y = x * inverse % P, y * inverse % P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        raise ValueError("not a curve point")
    return P - x if x & 1 != sign else x


_BY = 4 * pow(5, P - 2, P) % P
_BX = _recover_x(_BY, 0)
BASE = (_BX, _BY, 1, _BX * _BY % P)


def _expand(seed):
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed):
    return _encode(_multiply(_expand(seed)[0], BASE))


def sign(seed, message):
    scalar, prefix = _expand(seed)
    public = _encode(_multiply(scalar, BASE))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % L
    encoded_r = _encode(_multiply(r, BASE))
    k = int.from_bytes(hashlib.sha512(encoded_r + public + message).digest(), "little") % L
    return encoded_r + ((r + k * scalar) % L).to_bytes(32, "little")


def on_curve(raw):
    """Match curve25519-dalek decompression, which PDA derivation must avoid."""
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    u = (y * y - 1) % P
    v = (D * y * y + 1) % P
    x2 = u * pow(v, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


def find_program_address(seeds, program):
    program = from_base58(program)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not on_curve(digest):
            return to_base58(digest), bump
    raise ValueError("no viable program address")


class Keypair:
    def __init__(self, secret):
        secret = bytes(secret)
        if len(secret) != 64 or public_key(secret[:32]) != secret[32:]:
            raise ValueError("expected a 64-byte Solana keypair")
        self.seed = secret[:32]
        self.public = secret[32:]

    @classmethod
    def from_json(cls, text):
        return cls(json.loads(text))

    @property
    def address(self):
        return to_base58(self.public)

    def sign(self, message):
        return sign(self.seed, message)


def compact(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def legacy_message(payer, instructions, blockhash):
    """instructions: [(program, [(address, signer, writable)], data)] using base58 addresses."""
    flags = {payer: [True, True]}
    for program, metas, _ in instructions:
        for address, signer, writable in metas:
            entry = flags.setdefault(address, [False, False])
            entry[0] |= signer
            entry[1] |= writable
        flags.setdefault(program, [False, False])
    # Same grouping and in-group ordering as the Solana SDK message compiler.
    order = [payer] + sorted((k for k in flags if k != payer),
                             key=lambda k: (not flags[k][0], not flags[k][1], from_base58(k)))
    signers = [k for k in order if flags[k][0]]
    header = bytes([
        len(signers),
        sum(1 for k in signers if not flags[k][1]),
        sum(1 for k in order if not flags[k][0] and not flags[k][1]),
    ])
    index = {k: i for i, k in enumerate(order)}
    body = bytearray(header + compact(len(order)))
    for key in order:
        body += from_base58(key)
    body += from_base58(blockhash) + compact(len(instructions))
    for program, metas, data in instructions:
        body += bytes([index[program]]) + compact(len(metas)) + bytes(index[a] for a, _, _ in metas)
        body += compact(len(data)) + data
    return bytes(body), signers


def transaction(payer, instructions, blockhash, keypairs):
    message, signers = legacy_message(payer, instructions, blockhash)
    by_address = {k.address: k for k in keypairs}
    if set(signers) != set(by_address):
        raise ValueError("transaction signers differ from supplied keypairs")
    return compact(len(signers)) + b"".join(by_address[k].sign(message) for k in signers) + message


def ed25519_instruction(public, signature, message):
    """The compact self-referencing form accepted by the gate."""
    offsets = struct.pack("<BB7H", 1, 0, 48, 0xFFFF, 16, 0xFFFF, 112, len(message), 0xFFFF)
    return ED25519_PROGRAM, [], offsets + public + signature + message


def anchor_discriminator(name):
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def attestation_payload(mode, target, nonce, buffer, anchor_slot, replay_slots, elf_sha256, diff_root,
                        replayed, max_cu_delta_bps, expires_at_slot):
    payload = (DOMAINS[mode] + from_base58(target) + struct.pack("<Q", nonce) + from_base58(buffer)
               + struct.pack("<QQ", anchor_slot, replay_slots) + bytes(elf_sha256) + bytes(diff_root)
               + struct.pack("<IIHQ", replayed, 0, max_cu_delta_bps, expires_at_slot))
    if len(payload) != 186:
        raise ValueError("attestation payload must be 186 bytes")
    return payload


def generate_keypair():
    seed = os.urandom(32)
    return Keypair(seed + public_key(seed))


if __name__ == "__main__":
    # `keygen` prints the secret keypair for a secret store on stdout and only
    # the public address on stderr.
    if sys.argv[1:] != ["keygen"]:
        raise SystemExit("usage: solana_tx.py keygen")
    generated = generate_keypair()
    print(json.dumps(list(generated.seed + generated.public), separators=(",", ":")))
    print(generated.address, file=sys.stderr)
