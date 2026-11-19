"""Check the verified CI artifacts against a finalized Solana Devnet bank."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
GATE = "38SJ9rbJgLtibh4bL1VyoGb5uMaEQtSPBJZbTFahYtmz"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def rpc(method, params):
    req = urllib.request.Request(ENDPOINT, json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }).encode(), {"Content-Type": "application/json", "User-Agent": "canary-upgrade-gate-ci"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]


def from_base58(text):
    number = 0
    for char in text:
        number = number * 58 + ALPHABET.index(char)
    return bytes(len(text) - len(text.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")


def to_base58(raw):
    number = int.from_bytes(raw, "big")
    chars = []
    while number:
        number, digit = divmod(number, 58)
        chars.append(ALPHABET[digit])
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + "".join(reversed(chars))


def check(directory):
    evidence = directory / "mvp/evidence"
    report = json.loads((evidence / "report.json").read_bytes())
    artifact = (directory / "sbpf-solana-solana/release/canary_upgrade_gate.so").read_bytes()
    if hashlib.sha256(artifact).hexdigest() != report["gate_sha256"]:
        raise ValueError("Downloaded gate differs from the artifact tested in LiteSVM")
    if rpc("getGenesisHash", []) != GENESIS:
        raise ValueError("Unexpected cluster identity")
    read = {"commitment": "finalized", "encoding": "base64"}
    loader = rpc("getAccountInfo", [LOADER, read])["value"]
    if not loader or not loader["executable"] or loader["owner"] != "NativeLoader1111111111111111111111111111111":
        raise ValueError("Devnet loader v3 is unavailable")
    bank = rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    slot = bank["context"]["slot"]
    # In this legacy format the three header bytes and a single-byte address
    # count precede the addresses, followed by the recent blockhash.
    message = bytearray((evidence / "vote-message.bin").read_bytes())
    if len(message) < 4 or message[0] != 1 or not 1 <= message[3] < 128:
        raise ValueError("Unexpected compact vote message format")
    offset = 4 + 32 * message[3]
    keys = [bytes(message[i:i+32]) for i in range(4, offset, 32)]
    blockhash = from_base58(bank["value"]["blockhash"])
    if len(message) <= offset + 32 or len(blockhash) != 32 or from_base58(GATE) not in keys:
        raise ValueError("Vote message does not identify the tested gate")
    message[offset:offset+32] = blockhash
    fee = rpc("getFeeForMessage", [base64.b64encode(message).decode(), {
        "commitment": "finalized", "minContextSlot": slot
    }])["value"]
    if not isinstance(fee, int) or fee <= 0:
        raise ValueError("Devnet did not quote a fee for the compact vote")
    gate = rpc("getAccountInfo", [GATE, dict(read, minContextSlot=slot)])["value"]
    deployed = gate is not None
    if deployed:
        if gate["owner"] != LOADER or not gate["executable"]:
            raise ValueError("Configured Devnet address is not an executable loader-v3 program")
        data = base64.b64decode(gate["data"][0], validate=True)
        if len(data) != 36 or data[:4] != (2).to_bytes(4, "little"):
            raise ValueError("Invalid loader-v3 Program record")
        address = to_base58(data[4:])
        program_data = rpc("getAccountInfo", [address, dict(read, minContextSlot=slot)])["value"]
        if not program_data or program_data["owner"] != LOADER:
            raise ValueError("Missing loader-owned ProgramData")
        data = base64.b64decode(program_data["data"][0], validate=True)
        if (data[:4] != (3).to_bytes(4, "little") or data[45:45+len(artifact)] != artifact
                or any(data[45+len(artifact):])):
            raise ValueError("Deployed Devnet program differs from the CI artifact")
    return {"test": "devnet-rpc-compatibility", "genesis_hash": GENESIS, "finalized_slot": slot,
            "compact_vote_fee_lamports": fee, "gate_program_id": GATE, "gate_deployed": deployed,
            "gate_artifact_sha256": report["gate_sha256"], "transactions_sent": 0}


if __name__ == "__main__":
    result = json.dumps(check(Path(sys.argv[1])), indent=2)
    Path("devnet-report.json").write_text(result + "\n", encoding="utf-8")
    print(result)
