"""Capture a bounded live state bundle without treating it as replay evidence."""
from base64 import b64decode
from hashlib import sha256
import struct

from devnet import LOADER, from_base58, to_base58
from live_capture import FINAL, GATE, block, capture, rpc

MAX_PRESTATE_BYTES = 8 * 1024 * 1024
MAX_ANCHOR_DRIFT = 32
PROPOSAL_TAG = sha256(b"account:UpgradeProposal").digest()[:8]


class PrestateRefused(ValueError):
    pass


def account_bytes(account, owner):
    if not account or account["owner"] != owner:
        raise PrestateRefused("unexpected account owner")
    return b64decode(account["data"][0], validate=True)


def proposal_details(account, event, gate):
    data = account_bytes(account, gate)
    if len(data) != 248 or not data.startswith(PROPOSAL_TAG) or data[247] != 0:
        raise PrestateRefused("expected a pending v2 upgrade proposal")
    nonce, = struct.unpack_from("<Q", data, 9)
    anchor, span = struct.unpack_from("<QQ", data, 173)
    if (to_base58(data[17:49]), nonce, anchor, span) != (
        event["target"], event["nonce"], event["anchor_slot"], event["replay_slots"]
    ):
        raise PrestateRefused("proposal differs from finalized event")
    return {
        "buffer": to_base58(data[57:89]),
        "deployment_slot": struct.unpack_from("<Q", data, 49)[0],
        "candidate_sha256": data[121:153].hex(),
        "source_commit": data[153:173].hex(),
        "expires_at": struct.unpack_from("<q", data, 231)[0],
    }


def program_data_address(account):
    data = account_bytes(account, LOADER)
    if not account["executable"] or len(data) != 36 or data[:4] != bytes([2, 0, 0, 0]):
        raise PrestateRefused("expected a loader-v3 executable program")
    return to_base58(data[4:])

def check_transaction_prestate(item, accounts):
    message = item["transaction"]["message"]
    loaded = item["meta"].get("loadedAddresses") or {}
    keys = set(message["accountKeys"])
    keys.update(loaded.get("writable", []))
    keys.update(loaded.get("readonly", []))
    lookups = message.get("addressTableLookups") or []
    if (sum(len(lookup["writableIndexes"]) for lookup in lookups) != len(loaded.get("writable", []))
            or sum(len(lookup["readonlyIndexes"]) for lookup in lookups) != len(loaded.get("readonly", []))):
        raise PrestateRefused("finalized transaction is missing resolved ALT addresses")
    keys.update(lookup["accountKey"] for lookup in lookups)
    # These runtime programs are supplied by LiteSVM. Other executable accounts
    # must be present in the captured state, just like transaction data accounts.
    keys.difference_update({
        "11111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
    })
    missing = sorted(keys - accounts.keys())
    if missing:
        raise PrestateRefused(f"initial state was not captured for {len(missing)} transaction account(s); first: {missing[0]}")


def preflight_traffic(event, bundle, limit):
    snapshot = dict(target=list(from_base58(event["target"])), anchor_slot=event["anchor_slot"],
                    replay_slots=event["replay_slots"])
    traffic = capture(snapshot, limit, validate_transaction=lambda item: check_transaction_prestate(
        item, bundle["accounts"]
    ))
    return len(traffic["records"])

def check_captured_accounts_unchanged(anchor, capture_slot, tracked):
    """Disprove possible writes between the inclusive window start and the capture bank."""
    slots = rpc("getBlocks", anchor, capture_slot, FINAL)
    if not slots or slots != sorted(set(slots)) or slots[-1] != capture_slot or slots[0] < anchor:
        raise PrestateRefused("finalized block coverage is incomplete")
    previous = None
    for slot in slots:
        confirmed = block(slot, "json")
        if not confirmed or (previous is None and confirmed["parentSlot"] >= anchor) or (
            previous is not None and confirmed["parentSlot"] != previous
        ):
            raise PrestateRefused("finalized block parent chain is incomplete")
        for tx in confirmed["transactions"]:
            meta = tx.get("meta")
            if meta is None:
                raise PrestateRefused("missing transaction metadata during live state compatibility check")
            message = tx["transaction"]["message"]
            keys = message["accountKeys"]
            header = message["header"]
            signers = header["numRequiredSignatures"]
            signed_writable = signers - header["numReadonlySignedAccounts"]
            unsigned_end = len(keys) - header["numReadonlyUnsignedAccounts"]
            if not 1 <= signers <= len(keys) or not 0 <= signed_writable <= signers <= unsigned_end:
                raise PrestateRefused("invalid RPC transaction header")
            if message.get("addressTableLookups") and "loadedAddresses" not in meta:
                raise PrestateRefused("missing resolved ALT addresses during live state compatibility check")
            loaded = meta.get("loadedAddresses") or {}
            # Failed transactions may still debit fees or change durable nonces.
            possible_writes = set(keys[:signed_writable] + keys[signers:unsigned_end] + loaded.get("writable", []))
            possible_writes.add(keys[0])
            if possible_writes.intersection(tracked):
                raise PrestateRefused(f"captured account could have changed before the snapshot at slot {slot}")
        previous = slot


def capture_prestate(event, seed_accounts, gate=GATE):
    anchor = event["anchor_slot"]
    options = dict(FINAL, encoding="base64", minContextSlot=anchor)
    target, proposal = event["target"], event["proposal"]
    owned = rpc("getProgramAccounts", target, dict(options, withContext=True))
    slot = owned["context"]["slot"]
    if not anchor <= slot <= anchor + MAX_ANCHOR_DRIFT:
        raise PrestateRefused("live state was captured too far from the replay anchor")
    options["minContextSlot"] = slot

    pointers = rpc("getMultipleAccounts", [target, proposal], options)
    if pointers["context"]["slot"] != slot:
        raise PrestateRefused("RPC advanced between owned-account and pointer reads")
    program, proposed = pointers["value"]
    parsed = proposal_details(proposed, event, gate)
    program_data = program_data_address(program)
    core = [target, proposal, parsed["buffer"], program_data]
    addresses = list(dict.fromkeys([*core, *seed_accounts]))
    fetched = rpc("getMultipleAccounts", addresses, options)
    if fetched["context"]["slot"] != slot or len(fetched["value"]) != len(addresses):
        raise PrestateRefused("RPC advanced or did not return all requested accounts")
    accounts = dict(zip(addresses, fetched["value"]))
    if accounts[target] != program or accounts[proposal] != proposed:
        raise PrestateRefused("program or proposal differed within the capture bank")

    old = account_bytes(accounts[program_data], LOADER)
    if (len(old) < 45 or old[:4] != bytes([3, 0, 0, 0])
            or struct.unpack_from("<Q", old, 4)[0] != parsed["deployment_slot"]):
        raise PrestateRefused("baseline deploy slot does not match the proposal")
    candidate = account_bytes(accounts[parsed["buffer"]], LOADER)
    if len(candidate) <= 37 or candidate[:5] != bytes([1, 0, 0, 0, 1]):
        raise PrestateRefused("expected a loader-v3 buffer with an authority")
    if old[12] != 1 or old[13:45] != candidate[5:37]:
        raise PrestateRefused("buffer and ProgramData authority differ")

    # Budget encoded account entries before merging them into the worker cache.
    captures = {entry["pubkey"]: entry["account"] for entry in owned["value"]}
    if len(captures) != len(owned["value"]) or any(a["owner"] != target for a in captures.values()):
        raise PrestateRefused("invalid program-owned account response")
    if any(key in captures and captures[key] != value for key, value in accounts.items()):
        raise PrestateRefused("overlapping snapshots disagree")
    captures.update(accounts)
    encoded_bytes = sum(len(key) + 128 + (len(value["data"][0]) if value is not None else 0)
                        for key, value in captures.items())
    if encoded_bytes > MAX_PRESTATE_BYTES:
        raise PrestateRefused("live state exceeds the per-proposal memory budget")
    # Exclude the gate's proposal record, which is metadata for this job.
    check_captured_accounts_unchanged(anchor, slot, captures.keys() - {proposal})
    return {
        **parsed, "capture_slot": slot, "anchor_slot": anchor, "accounts": captures,
        "encoded_bytes": encoded_bytes,
    }
