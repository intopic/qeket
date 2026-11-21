"""Capture a bounded live state bundle and turn a finalized window into a replay archive."""
from base64 import b64decode
from hashlib import sha256
import struct

from devnet import LOADER, from_base58, to_base58
from live_capture import FINAL, GATE, block, capture, invoked, rpc
from solana_tx import find_program_address

MAX_PRESTATE_BYTES = 8 * 1024 * 1024
MAX_ANCHOR_DRIFT = 32
PROPOSAL_TAG = sha256(b"account:UpgradeProposal").digest()[:8]
CONFIG_TAG = sha256(b"account:GatekeeperConfig").digest()[:8]
SYSTEM = "11111111111111111111111111111111"
SYSVAR_OWNER = "Sysvar1111111111111111111111111111111111111"
CLOCK = "SysvarC1ock11111111111111111111111111111111"
INSTRUCTIONS = "Sysvar1nstructions1111111111111111111111111"
# LiteSVM supplies these. The archive supplies Clock for each slot and the
# instructions sysvar is derived from the transaction itself.
RUNTIME_ACCOUNTS = {SYSTEM, "ComputeBudget111111111111111111111111111111", CLOCK, INSTRUCTIONS}
REPLAY_MODES = ("StrictEquality", "NoRevertAndCuBound")
RENT_EXEMPT_EPOCH = 2 ** 64 - 1


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


def config_details(account, target, gate):
    """Policy fields the verifiers must replay under, read from the gate config."""
    data = account_bytes(account, gate)
    if len(data) != 371 or not data.startswith(CONFIG_TAG) or data[8] != 2 or to_base58(data[10:42]) != target:
        raise PrestateRefused("expected the v2 gate config for this target")
    mode, count, threshold = data[106], data[331], data[332]
    if mode >= len(REPLAY_MODES) or not 2 <= threshold <= count <= 7:
        raise PrestateRefused("invalid gate policy")
    max_cu, min_txs, min_slots = struct.unpack_from("<HIQ", data, 333)
    return {
        "replay_mode": REPLAY_MODES[mode],
        "verifiers": [to_base58(data[107 + 32 * i:139 + 32 * i]) for i in range(count)],
        "threshold": threshold,
        "max_cu_delta_bps": max_cu,
        "min_replay_txs": min_txs,
        "min_replay_slots": min_slots,
        "proposal_nonce": struct.unpack_from("<Q", data, 363)[0],
    }


def clock_details(account, slot):
    data = account_bytes(account, SYSVAR_OWNER)
    if len(data) != 40:
        raise PrestateRefused("invalid Clock sysvar")
    fields = dict(zip(("slot", "epoch_start_timestamp", "epoch", "leader_schedule_epoch", "unix_timestamp"),
                      struct.unpack("<QqQQq", data)))
    if fields["slot"] != slot:
        raise PrestateRefused("Clock sysvar differs from the capture bank")
    return fields


def program_data_address(account):
    data = account_bytes(account, LOADER)
    if not account["executable"] or len(data) != 36 or data[:4] != bytes([2, 0, 0, 0]):
        raise PrestateRefused("expected a loader-v3 executable program")
    return to_base58(data[4:])


def resolved_addresses(item):
    message = item["transaction"]["message"]
    loaded = item["meta"].get("loadedAddresses") or {}
    lookups = message.get("addressTableLookups") or []
    if (sum(len(lookup["writableIndexes"]) for lookup in lookups) != len(loaded.get("writable", []))
            or sum(len(lookup["readonlyIndexes"]) for lookup in lookups) != len(loaded.get("readonly", []))):
        raise PrestateRefused("finalized transaction is missing resolved ALT addresses")
    return loaded.get("writable", []), loaded.get("readonly", []), lookups


def check_transaction_prestate(item, accounts, fee_payers=()):
    """Return the accounts a transaction needs. Unknown fee payers are reconstructed later."""
    message = item["transaction"]["message"]
    writable, readonly, lookups = resolved_addresses(item)
    keys = {*message["accountKeys"], *writable, *readonly}
    keys.update(lookup["accountKey"] for lookup in lookups)
    # These runtime programs are supplied by LiteSVM. Other executable accounts
    # must be present in the captured state, just like transaction data accounts.
    keys.difference_update(RUNTIME_ACCOUNTS)
    missing = sorted(keys - accounts.keys() - set(fee_payers))
    if missing:
        raise PrestateRefused(f"initial state was not captured for {len(missing)} transaction account(s); first: {missing[0]}")
    for key in sorted(keys):
        account = accounts.get(key)
        if account is not None and account["executable"] and account["owner"] == LOADER:
            program_data = program_data_address(account)
            account_bytes(accounts.get(program_data), LOADER)
            keys.add(program_data)
    return keys


def check_window_epoch(anchor, end):
    schedule = rpc("getEpochSchedule")
    first = schedule["firstNormalSlot"]
    size = schedule["slotsPerEpoch"]
    if size <= 0 or anchor < first or (anchor - first) // size != (end - first) // size:
        raise PrestateRefused("replay window crosses an epoch boundary or is in epoch warmup")
    return schedule["firstNormalEpoch"] + (anchor - first) // size


def possible_writes(tx):
    meta = tx.get("meta")
    if meta is None:
        raise PrestateRefused("missing transaction metadata during live state compatibility check")
    message = tx["transaction"]["message"]
    keys = message["accountKeys"]
    header = message["header"]
    signers = header["numRequiredSignatures"]
    signed_writable = signers - header["numReadonlySignedAccounts"]
    unsigned_end = len(keys) - header["numReadonlyUnsignedAccounts"]
    if not 1 <= signers <= len(keys) or not 0 <= signed_writable <= signers <= unsigned_end <= len(keys):
        raise PrestateRefused("invalid RPC transaction header")
    loaded_writable, _, _ = resolved_addresses(tx)
    # Failed transactions may still debit the payer or change a durable nonce.
    return {keys[0], *keys[:signed_writable], *keys[signers:unsigned_end], *loaded_writable}


def finalized_blocks(anchor, slots):
    if not slots or slots != sorted(set(slots)) or slots[0] < anchor:
        raise PrestateRefused("finalized block coverage is incomplete")
    previous = None
    for slot in slots:
        confirmed = block(slot, "json")
        if not confirmed or (previous is None and confirmed["parentSlot"] >= anchor) or (
            previous is not None and confirmed["parentSlot"] != previous
        ):
            raise PrestateRefused("finalized block parent chain is incomplete")
        yield slot, confirmed
        previous = slot


def check_initial_balances(tx, accounts, observed, synthesized=None):
    """Compare first-use balances. Unknown fee payers take their first observed balance."""
    meta = tx["meta"]
    message = tx["transaction"]["message"]
    writable, readonly, _ = resolved_addresses(tx)
    keys = message["accountKeys"] + writable + readonly
    balances = meta["preBalances"]
    if len(keys) != len(balances):
        raise PrestateRefused("finalized balance metadata does not match transaction account keys")
    for key, balance in zip(keys, balances):
        if key in observed:
            continue
        if key in accounts:
            account = accounts[key]
            expected = 0 if account is None else account["lamports"]
            if balance != expected:
                raise PrestateRefused(f"first observed balance differs from captured account {key}")
            observed.add(key)
        elif synthesized is not None and key not in RUNTIME_ACCOUNTS:
            if type(balance) is not int or balance <= 0:
                raise PrestateRefused(f"fee payer {key} has no observable starting balance")
            synthesized[key] = balance
            observed.add(key)


def check_window_isolation(event, records, tracked, accounts, synthesized=None):
    """Scan every finalized block of the window. Return production times by slot."""
    anchor = event["anchor_slot"]
    end = anchor + event["replay_slots"]
    slots = rpc("getBlocks", anchor, end, FINAL)
    next_slots = rpc("getBlocksWithLimit", end + 1, 1, FINAL)
    if not next_slots:
        raise RuntimeError("waiting for a finalized block after the replay window")
    if len(next_slots) != 1 or next_slots[0] <= end or any(slot > end for slot in slots):
        raise PrestateRefused("invalid finalized window boundaries")
    expected = {(record["slot"], record["index"]): to_base58(bytes.fromhex(record["transaction"])[1:65])
                for record in records}
    if len(expected) != len(records):
        raise PrestateRefused("duplicate replay transaction positions")
    observed = set()
    times = {}
    parent = None
    for slot, confirmed in finalized_blocks(anchor, slots + next_slots):
        if slot > end:
            break
        if parent is None:
            parent = confirmed["parentSlot"]
        times[slot] = confirmed.get("blockTime")
        for index, tx in enumerate(confirmed["transactions"]):
            position = (slot, index)
            signature = expected.pop(position, None)
            if invoked(tx, event["target"]):
                if signature is None or tx["transaction"]["signatures"][0] != signature:
                    raise PrestateRefused("target transaction list differs from the finalized block")
                check_initial_balances(tx, accounts, observed, synthesized)
            elif signature is not None:
                raise PrestateRefused("target transaction list differs from the finalized block")
            elif possible_writes(tx).intersection(tracked):
                raise PrestateRefused("captured account modified by non-target transaction during replay window")
    if expected:
        raise PrestateRefused("target replay transaction was absent from the finalized window")
    return {"parent_slot": parent, "block_times": times}


def check_captured_accounts_unchanged(anchor, capture_slot, tracked):
    """Disprove possible writes between the inclusive window start and the capture bank."""
    slots = rpc("getBlocks", anchor, capture_slot, FINAL)
    if not slots or slots[-1] != capture_slot:
        raise PrestateRefused("finalized block coverage is incomplete")
    for slot, confirmed in finalized_blocks(anchor, slots):
        for tx in confirmed["transactions"]:
            if possible_writes(tx).intersection(tracked):
                raise PrestateRefused(f"captured account could have changed before the snapshot at slot {slot}")


def window_clocks(event, bundle, epoch, scan):
    """One Clock per slot. Skipped slots carry the last produced timestamp."""
    clock = bundle["clock"]
    if clock["epoch"] != epoch:
        raise PrestateRefused("live state was captured in a different epoch than the replay window")
    times = scan["block_times"]
    if any(type(value) is not int for value in times.values()):
        raise PrestateRefused("finalized block time is unavailable")
    timestamp = None
    if event["anchor_slot"] not in times:
        timestamp = rpc("getBlockTime", scan["parent_slot"])
        if type(timestamp) is not int:
            raise PrestateRefused("finalized block time is unavailable")
    clocks = []
    for slot in range(event["anchor_slot"], event["anchor_slot"] + event["replay_slots"] + 1):
        timestamp = times.get(slot, timestamp)
        clocks.append(dict(slot=slot, epoch_start_timestamp=clock["epoch_start_timestamp"], epoch=epoch,
                           leader_schedule_epoch=clock["leader_schedule_epoch"], unix_timestamp=timestamp))
    return clocks


def archive_account(account):
    return dict(lamports=account["lamports"], data=list(b64decode(account["data"][0], validate=True)),
                owner=list(from_base58(account["owner"])), executable=account["executable"],
                rentEpoch=account["rentEpoch"])


def system_account(lamports):
    # A fee payer must be a System account. A durable-nonce payer would differ in
    # data, which the baseline balance and compute checks surface during replay.
    return dict(lamports=lamports, data=[], owner=list(bytes(32)), executable=False, rentEpoch=RENT_EXEMPT_EPOCH)


def replay_archive(event, bundle, limit):
    """Build the archive `canary-gate verify` consumes from the finalized window."""
    anchor = event["anchor_slot"]
    span = event["replay_slots"]
    epoch = check_window_epoch(anchor, anchor + span)
    snapshot = dict(target=list(from_base58(event["target"])), anchor_slot=anchor, replay_slots=span)
    items = []
    captured = capture(snapshot, limit, validate_transaction=items.append, include_baseline=True)
    records = captured["records"]
    if not records:
        raise PrestateRefused("replay window contains no successful target transactions")
    accounts = bundle["accounts"]
    payers = {item["transaction"]["message"]["accountKeys"][0] for item in items} - accounts.keys()
    tracked, direct = set(), set()
    for item in items:
        tracked.update(check_transaction_prestate(item, accounts, payers))
        message = item["transaction"]["message"]
        writable, readonly, lookups = resolved_addresses(item)
        direct.update(message["accountKeys"], writable, readonly, (lookup["accountKey"] for lookup in lookups))
    synthesized = {}
    scan = check_window_isolation(event, records, tracked, accounts, synthesized)
    if synthesized.keys() != payers:
        raise PrestateRefused("fee payer starting balance was not observed")
    # The runner installs the baseline ELF at the target address itself.
    needed = tracked - RUNTIME_ACCOUNTS - {event["target"]}
    if bundle["program_data"] not in direct:
        needed.discard(bundle["program_data"])
    present, absent = [], []
    for key in sorted(needed, key=from_base58):
        value = system_account(synthesized[key]) if key in synthesized else accounts[key]
        if value is None:
            absent.append(list(from_base58(key)))
        else:
            present.append([list(from_base58(key)), value if key in synthesized else archive_account(value)])
    policy = bundle["config"]
    return dict(
        format_version=2, replay_mode=policy["replay_mode"], absent_accounts=absent, slot_hashes=[],
        transaction_manifest=captured["transaction_manifest"], target=list(from_base58(event["target"])),
        gate=list(from_base58(bundle["config_address"])), buffer_key=list(from_base58(bundle["buffer"])),
        nonce=event["nonce"], source_commit=bundle["source_commit"],
        expected_elf=list(bytes.fromhex(bundle["candidate_sha256"])),
        baseline_elf=list(sha256(baseline_elf(bundle)).digest()),
        buffer=archive_account(accounts[bundle["buffer"]]), anchor_slot=anchor, replay_slots=span,
        min_txs=policy["min_replay_txs"], cu_limit_bps=policy["max_cu_delta_bps"],
        accounts=present, clocks=window_clocks(event, bundle, epoch, scan), records=records,
    )


def preflight_traffic(event, bundle, limit):
    return len(replay_archive(event, bundle, limit)["records"])


def baseline_elf(bundle):
    """The loader executes the whole ProgramData body, including zero padding."""
    return account_bytes(bundle["accounts"][bundle["program_data"]], LOADER)[45:]


def read_slot(result, floor, anchor):
    slot = result["context"]["slot"]
    if slot < floor:
        raise PrestateRefused("RPC returned an older bank than a previous capture read")
    if slot > anchor + MAX_ANCHOR_DRIFT:
        raise PrestateRefused("live state was captured too far from the replay anchor")
    return slot


def capture_prestate(event, seed_accounts, gate=GATE):
    anchor = event["anchor_slot"]
    options = dict(FINAL, encoding="base64", minContextSlot=anchor)
    target, proposal = event["target"], event["proposal"]
    # A public RPC finalizes a new bank every few hundred milliseconds, so the
    # three reads may land in successive banks. Scanning every block from the
    # anchor through the latest read proves each read equals the state at the
    # start of the anchor slot.
    owned = rpc("getProgramAccounts", target, dict(options, withContext=True))
    slot = read_slot(owned, anchor, anchor)
    options["minContextSlot"] = slot

    pointers = rpc("getMultipleAccounts", [target, proposal], options)
    slot = read_slot(pointers, slot, anchor)
    options["minContextSlot"] = slot
    program, proposed = pointers["value"]
    parsed = proposal_details(proposed, event, gate)
    program_data = program_data_address(program)
    config_address, _ = find_program_address([b"gatekeeper_cfg", from_base58(target)], gate)
    core = [target, proposal, parsed["buffer"], program_data]
    addresses = list(dict.fromkeys([*core, *seed_accounts]))
    fetched = rpc("getMultipleAccounts", [*addresses, config_address, CLOCK], options)
    slot = read_slot(fetched, slot, anchor)
    if len(fetched["value"]) != len(addresses) + 2:
        raise PrestateRefused("RPC did not return all requested accounts")
    *values, config_account, clock_account = fetched["value"]
    accounts = dict(zip(addresses, values))
    if accounts[target] != program or accounts[proposal] != proposed:
        raise PrestateRefused("program or proposal changed during the capture")
    policy = config_details(config_account, target, gate)
    clock = clock_details(clock_account, slot)

    old = account_bytes(accounts[program_data], LOADER)
    if (len(old) < 45 or old[:4] != bytes([3, 0, 0, 0])
            or struct.unpack_from("<Q", old, 4)[0] != parsed["deployment_slot"]):
        raise PrestateRefused("baseline deploy slot does not match the proposal")
    candidate = account_bytes(accounts[parsed["buffer"]], LOADER)
    if len(candidate) <= 37 or candidate[:5] != bytes([1, 0, 0, 0, 1]):
        raise PrestateRefused("expected a loader-v3 buffer with an authority")
    if old[12] != 1 or old[13:45] != candidate[5:37]:
        raise PrestateRefused("buffer and ProgramData authority differ")
    if to_base58(candidate[5:37]) != config_address:
        raise PrestateRefused("the gate config does not hold the upgrade authority")

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
        "encoded_bytes": encoded_bytes, "program_data": program_data, "config_address": config_address,
        "config": policy, "clock": clock,
    }
