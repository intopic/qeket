"""Capture a bounded live state bundle and turn a finalized window into a replay archive."""
from base64 import b64decode, b64encode
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
FEATURE_OWNER = "Feature111111111111111111111111111111111111"
ALPENGLOW_FEATURE = "A1pengvuM6JEcyNuTnMqepBKhwHE3N6PmUrdATGawhJS"
RENT_EXEMPT_EPOCH = 2 ** 64 - 1
NATIVE_LOADER = "NativeLoader1111111111111111111111111111111"
# The loaders whose programs the runner installs from captured accounts.
PROGRAM_LOADERS = {LOADER, "BPFLoader2111111111111111111111111111111111", "BPFLoader1111111111111111111111111111111111"}
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
# Transfer, MintTo, Burn, TransferChecked, MintToChecked and BurnChecked change
# only a token account's amount, and its lamports for native SOL. Any other
# instruction can change fields that no transaction metadata records.
AMOUNT_ONLY = {3, 7, 8, 12, 14, 15}
# For a debit (Transfer, Burn and their Checked variants): the position of the
# debited account and of its authority. A delegate as authority also lowers
# delegated_amount and can clear the delegate, which metadata does not record.
DEBIT_AUTHORITY = {3: (0, 2), 12: (0, 3), 8: (0, 2), 15: (0, 2)}
# Token-2022 account extensions that transfers, mints and burns leave unchanged:
# ImmutableOwner, MemoTransfer, CpiGuard and NonTransferableAccount. Others, such
# as TransferFeeAmount, change with a transfer.
STATIC_EXTENSIONS = {7, 8, 11, 13}
MAX_LATE_BYTES = 16 * 1024 * 1024


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


def ordered_keys(item):
    """Account keys in the index space of compiled instructions: static, then loaded."""
    writable, readonly, _ = resolved_addresses(item)
    return item["transaction"]["message"]["accountKeys"] + writable + readonly


def referenced_accounts(item):
    _, _, lookups = resolved_addresses(item)
    return {*ordered_keys(item), *(lookup["accountKey"] for lookup in lookups)}


def check_transaction_prestate(item, accounts, fee_payers=(), runtime=RUNTIME_ACCOUNTS):
    """Return the accounts a transaction needs. Unknown fee payers are reconstructed later."""
    message = item["transaction"]["message"]
    writable, readonly, lookups = resolved_addresses(item)
    ordered = message["accountKeys"] + writable + readonly
    # Only the fee debit can be reconstructed. A program receiving this
    # account can observe or change fields that preBalances does not record.
    unknown = set(fee_payers)
    if unknown:
        referenced = set(ordered[1:]) | {lookup["accountKey"] for lookup in lookups}
        for ix in message["instructions"]:
            for index in [ix["programIdIndex"], *ix["accounts"]]:
                if type(index) is not int or not 0 <= index < len(ordered):
                    raise PrestateRefused("invalid compiled instruction account index")
                referenced.add(ordered[index])
        if unknown.intersection(referenced):
            raise PrestateRefused("mixed-role fee payer requires captured initial state")
    keys = {*message["accountKeys"], *writable, *readonly}
    keys.update(lookup["accountKey"] for lookup in lookups)
    # These runtime programs are supplied by LiteSVM. Other executable accounts
    # must be present in the captured state, just like transaction data accounts.
    keys.difference_update(runtime)
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


def epoch_of(slot, schedule):
    return schedule["firstNormalEpoch"] + (slot - schedule["firstNormalSlot"]) // schedule["slotsPerEpoch"]


def check_window_epoch(anchor, end):
    schedule = rpc("getEpochSchedule")
    first = schedule["firstNormalSlot"]
    size = schedule["slotsPerEpoch"]
    if size <= 0 or anchor < first or (anchor - first) // size != (end - first) // size:
        raise PrestateRefused("replay window crosses an epoch boundary or is in epoch warmup")
    return epoch_of(anchor, schedule)


def parent_clock(anchor, epoch):
    """True when transactions in slot S read blockTime(parent(S)) instead of blockTime(S).

    Under Alpenglow the bank keeps the parent footer time while transactions
    run, and blockTime(S) is the footer time of S itself (anza-xyz/agave#15441).
    """
    value = rpc("getAccountInfo", ALPENGLOW_FEATURE, dict(FINAL, encoding="base64"))["value"]
    if value is None:
        return False
    data = account_bytes(value, FEATURE_OWNER)
    if len(data) < 9 or data[0] != 1:
        return False
    activated, = struct.unpack_from("<Q", data, 1)
    # The switch slot follows neither rule. Stay clear of the activation epoch.
    if activated > anchor or epoch <= epoch_of(activated, rpc("getEpochSchedule")) + 1:
        raise PrestateRefused("replay window is too close to the Alpenglow activation")
    return True


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


def check_initial_balances(tx, accounts, observed, synthesized=None, runtime=RUNTIME_ACCOUNTS):
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
        elif synthesized is not None and key not in runtime:
            if type(balance) is not int or balance <= 0:
                raise PrestateRefused(f"fee payer {key} has no observable starting balance")
            synthesized[key] = balance
            observed.add(key)


def check_window_isolation(event, records, tracked, accounts, synthesized=None, runtime=RUNTIME_ACCOUNTS):
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
    times, parents = {}, {}
    parent = None
    for slot, confirmed in finalized_blocks(anchor, slots + next_slots):
        if slot > end:
            break
        if parent is None:
            parent = confirmed["parentSlot"]
        times[slot] = confirmed.get("blockTime")
        parents[slot] = confirmed["parentSlot"]
        for index, tx in enumerate(confirmed["transactions"]):
            position = (slot, index)
            signature = expected.pop(position, None)
            if invoked(tx, event["target"]):
                if signature is None or tx["transaction"]["signatures"][0] != signature:
                    raise PrestateRefused("target transaction list differs from the finalized block")
                check_initial_balances(tx, accounts, observed, synthesized, runtime)
            elif signature is not None:
                raise PrestateRefused("target transaction list differs from the finalized block")
            elif possible_writes(tx).intersection(tracked):
                raise PrestateRefused("captured account modified by non-target transaction during replay window")
    if expected:
        raise PrestateRefused("target replay transaction was absent from the finalized window")
    return {"parent_slot": parent, "block_times": times, "parents": parents}


def check_captured_accounts_unchanged(anchor, capture_slot, tracked):
    """Disprove possible writes between the inclusive window start and the capture bank."""
    slots = rpc("getBlocks", anchor, capture_slot, FINAL)
    if not slots or slots[-1] != capture_slot:
        raise PrestateRefused("finalized block coverage is incomplete")
    for slot, confirmed in finalized_blocks(anchor, slots):
        for tx in confirmed["transactions"]:
            if possible_writes(tx).intersection(tracked):
                raise PrestateRefused(f"captured account could have changed before the snapshot at slot {slot}")


def window_clocks(event, bundle, epoch, scan, from_parent=False):
    """One Clock per slot, as the transactions in that slot read it.

    TowerBFT: blockTime of the slot. Alpenglow: blockTime of its parent block.
    No transaction runs in a skipped slot, so it carries the last value.
    """
    clock = bundle["clock"]
    if clock["epoch"] != epoch:
        raise PrestateRefused("live state was captured in a different epoch than the replay window")
    produced = scan["block_times"]
    if any(type(value) is not int for value in produced.values()):
        raise PrestateRefused("finalized block time is unavailable")
    times = dict(produced)
    timestamp = None
    if from_parent or event["anchor_slot"] not in produced:
        timestamp = rpc("getBlockTime", scan["parent_slot"])
        if type(timestamp) is not int:
            raise PrestateRefused("finalized block time is unavailable")
        times.setdefault(scan["parent_slot"], timestamp)
    clocks = []
    for slot in range(event["anchor_slot"], event["anchor_slot"] + event["replay_slots"] + 1):
        if slot in produced:
            timestamp = times[scan["parents"][slot]] if from_parent else produced[slot]
        clocks.append(dict(slot=slot, epoch_start_timestamp=clock["epoch_start_timestamp"], epoch=epoch,
                           leader_schedule_epoch=clock["leader_schedule_epoch"], unix_timestamp=timestamp))
    return clocks


def archive_account(account):
    return dict(lamports=account["lamports"], data=list(b64decode(account["data"][0], validate=True)),
                owner=list(from_base58(account["owner"])), executable=account["executable"],
                rentEpoch=account["rentEpoch"])


def read_after_window(keys, end):
    """Finalized states read after the window, with the bank slot of each read."""
    keys = sorted(keys)
    states, read_slots = {}, {}
    for start in range(0, len(keys), 100):
        batch = keys[start:start + 100]
        result = rpc("getMultipleAccounts", batch, dict(FINAL, encoding="base64", minContextSlot=end + 1))
        slot = result["context"]["slot"]
        if slot <= end or len(result["value"]) != len(batch):
            raise PrestateRefused("incomplete finalized account state after the window")
        for key, value in zip(batch, result["value"]):
            states[key] = value
            read_slots[key] = slot
    return states, read_slots


def check_untouched_after(read_slots, end):
    """Disprove writes to each account between the window end and its read."""
    if not read_slots:
        return
    slots = rpc("getBlocks", end + 1, max(read_slots.values()), FINAL)
    if not set(read_slots.values()).issubset(slots):
        raise PrestateRefused("finalized block coverage is incomplete")
    for slot, confirmed in finalized_blocks(end + 1, slots):
        for tx in confirmed["transactions"]:
            if any(read_slots.get(key, -1) >= slot for key in possible_writes(tx)):
                raise PrestateRefused("an account read after the window was used again before the read")


def payer_final_state(key, value):
    if value is None or account_bytes(value, SYSTEM) or value["executable"]:
        raise PrestateRefused(f"fee payer {key} requires captured initial state")
    return [list(from_base58(key)), archive_account(value)]


def final_states(keys, end):
    """Read reconstructed fee payers after the window and disprove writes since it ended.

    The runner checks that the baseline replay ends in this state, which turns the
    assumed starting state of each fee payer into a checked one.
    """
    states, read_slots = read_after_window(keys, end)
    finals = [payer_final_state(key, states[key]) for key in sorted(states)]
    check_untouched_after(read_slots, end)
    return finals


def instructions_of(item):
    """Top-level and inner instructions, all in the transaction's account index space."""
    inner = item["meta"].get("innerInstructions")
    if inner is None:
        raise PrestateRefused("finalized transaction metadata lacks inner instructions")
    yield from item["transaction"]["message"]["instructions"]
    for group in inner:
        yield from group["instructions"]


def check_amount_only(items, key, program, owner):
    """Refuse a token account that any window instruction can change beyond its amount."""
    for item in items:
        keys = ordered_keys(item)
        if key not in keys:
            continue
        index = keys.index(key)
        for ix in instructions_of(item):
            if index not in ix["accounts"] or keys[ix["programIdIndex"]] not in TOKEN_PROGRAMS:
                # Other programs can only read the account or credit its lamports.
                continue
            data = from_base58(ix["data"])
            if keys[ix["programIdIndex"]] != program or not data or data[0] not in AMOUNT_ONLY:
                raise PrestateRefused(f"token account {key} is used by an instruction other than transfer, "
                                      f"mint or burn")
            debited, authority = DEBIT_AUTHORITY.get(data[0], (None, None))
            accounts = ix["accounts"]
            if debited is not None and accounts[debited:debited + 1] == [index] and (
                    len(accounts) <= authority or keys[accounts[authority]] != owner):
                raise PrestateRefused(f"token account {key} is debited by a delegate or another authority")


def check_static_extensions(key, data):
    """Refuse Token-2022 extensions that a transfer, mint or burn can change."""
    offset = 166
    while len(data) > 165 and offset + 4 <= len(data):
        kind, length = struct.unpack_from("<HH", data, offset)
        if kind == 0:
            break  # Uninitialized: the rest is padding.
        if kind not in STATIC_EXTENSIONS or offset + 4 + length > len(data):
            raise PrestateRefused(f"token account {key} has extension {kind}, which the rebuild does not support")
        offset += 4 + length


def rebuild_token_account(items, key, final):
    """The state of a token account at its first use in the window.

    The amount and lamports come from the first transaction's finalized metadata.
    Every other field comes from the state read after the window, which
    `check_amount_only` shows no window instruction changed. The runner then
    requires the baseline replay to end in that final state.
    """
    if final is None or final["owner"] not in TOKEN_PROGRAMS or final["executable"]:
        raise PrestateRefused(f"account {key} is written by the window but was not captured at the anchor")
    data = bytearray(b64decode(final["data"][0], validate=True))
    # Base layout: mint, owner, amount, delegate, state at byte 108. Token-2022
    # extensions follow an account type byte, 2 for a token account.
    if len(data) < 165 or (len(data) > 165 and data[165] != 2) or data[108] != 1:
        raise PrestateRefused(f"account {key} is not an initialized, unfrozen token account")
    check_static_extensions(key, data)
    check_amount_only(items, key, final["owner"], to_base58(data[32:64]))
    first = next(item for item in items if key in ordered_keys(item))
    index = ordered_keys(first).index(key)
    balances = [entry for entry in first["meta"].get("preTokenBalances") or [] if entry["accountIndex"] == index]
    if len(balances) != 1 or "owner" not in balances[0]:
        raise PrestateRefused(f"token account {key} has no observed starting balance")
    balance, = balances
    if (to_base58(data[:32]) != balance["mint"] or to_base58(data[32:64]) != balance["owner"]
            or balance.get("programId", final["owner"]) != final["owner"]):
        raise PrestateRefused(f"token account {key} changed mint, owner or program during the window")
    data[64:72] = int(balance["uiTokenAmount"]["amount"]).to_bytes(8, "little")
    # Same shape as an RPC account, like every other entry of the capture.
    return dict(lamports=first["meta"]["preBalances"][index], data=[b64encode(bytes(data)).decode(), "base64"],
                owner=final["owner"], executable=False, rentEpoch=final["rentEpoch"])


def late_accounts(items, accounts, payers, end):
    """Read, after the window, every account the window uses that the anchor capture lacks.

    - Fee payers start as System accounts with their first observed balance.
    - An account no target transaction writes, and that nothing else wrote from the
      anchor to its read, had the read state when the window used it. Programs and
      their ProgramData come in this way, with the bytes the cluster executes.
    - A token account that the target transactions only move tokens in is rebuilt
      at its first use.
    The runner checks fee payers and rebuilt token accounts against their final state.
    Writes inside the window by other transactions are refused by the window scan,
    which receives these accounts as tracked.
    """
    referenced, written = set(), set()
    for item in items:
        referenced |= referenced_accounts(item)
        written |= possible_writes(item)
    missing = referenced - RUNTIME_ACCOUNTS - accounts.keys() - payers
    states, read_slots = read_after_window(missing | payers, end)
    initial, finals, builtins = {}, [], set()
    for key in sorted(payers):
        finals.append(payer_final_state(key, states[key]))
    for key in sorted(missing - written):
        value = states[key]
        if value is not None and value["owner"] == NATIVE_LOADER:
            builtins.add(key)  # LiteSVM supplies builtin programs and precompiles.
        elif value is not None and value["owner"] == SYSVAR_OWNER:
            raise PrestateRefused(f"the replay does not supply sysvar {key}")
        elif value is not None and value["executable"] and value["owner"] not in PROGRAM_LOADERS:
            raise PrestateRefused(f"program {key} uses a loader the replay does not support")
        else:
            initial[key] = value
    for key in sorted(missing & written):
        initial[key] = rebuild_token_account(items, key, states[key])
        finals.append([list(from_base58(key)), archive_account(states[key])])
    program_data = {program_data_address(value) for value in initial.values()
                    if value is not None and value["executable"] and value["owner"] == LOADER}
    extra, extra_slots = read_after_window(program_data - accounts.keys() - initial.keys(), end)
    initial.update(extra)
    read_slots.update(extra_slots)
    size = sum(len(value["data"][0]) * 3 // 4 for value in initial.values() if value is not None)
    if size > MAX_LATE_BYTES:
        raise PrestateRefused("accounts read after the window exceed the archive budget")
    check_untouched_after(read_slots, end)
    return dict(initial=initial, finals=finals, builtins=builtins)


def active_features(anchor):
    """Features the cluster had activated at the anchor, with their activation slots.

    Features take effect at epoch boundaries and the window stays inside one epoch.
    """
    entries = rpc("getProgramAccounts", FEATURE_OWNER, dict(FINAL, encoding="base64", minContextSlot=anchor))
    features = []
    for entry in entries:
        data = account_bytes(entry["account"], FEATURE_OWNER)
        if len(data) >= 9 and data[0] == 1:
            slot, = struct.unpack_from("<Q", data, 1)
            if slot <= anchor:
                features.append([list(from_base58(entry["pubkey"])), slot])
        elif not data or data[0] != 0:
            raise PrestateRefused(f"invalid feature account {entry['pubkey']}")
    if not features:
        raise PrestateRefused("the cluster reported no active features")
    return sorted(features)


def system_account(lamports):
    # The runtime accepts only System fee payers with no data or nonce data.
    # `final_states` checks the no-data assumption against the chain.
    return dict(lamports=lamports, data=[], owner=list(bytes(32)), executable=False, rentEpoch=RENT_EXEMPT_EPOCH)


def replay_archive(event, bundle, limit):
    """Build the archive `canary-gate verify` consumes from the finalized window."""
    anchor = event["anchor_slot"]
    span = event["replay_slots"]
    epoch = check_window_epoch(anchor, anchor + span)
    from_parent = parent_clock(anchor, epoch)
    snapshot = dict(target=list(from_base58(event["target"])), anchor_slot=anchor, replay_slots=span)
    items = []
    captured = capture(snapshot, limit, validate_transaction=items.append, include_baseline=True)
    records = captured["records"]
    if not records:
        raise PrestateRefused("replay window contains no successful target transactions")
    # Block order. The first use of an account decides its rebuilt starting state.
    order = {to_base58(bytes.fromhex(record["transaction"])[1:65]): (record["slot"], record["index"])
             for record in records}
    items.sort(key=lambda item: order[item["transaction"]["signatures"][0]])
    payers = {item["transaction"]["message"]["accountKeys"][0] for item in items} - bundle["accounts"].keys()
    late = late_accounts(items, bundle["accounts"], payers, anchor + span)
    accounts = {**bundle["accounts"], **late["initial"]}
    runtime = RUNTIME_ACCOUNTS | late["builtins"]
    tracked, direct = set(), set()
    for item in items:
        tracked.update(check_transaction_prestate(item, accounts, payers, runtime))
        direct.update(referenced_accounts(item))
    synthesized = {}
    scan = check_window_isolation(event, records, tracked, accounts, synthesized, runtime)
    if synthesized.keys() != payers:
        raise PrestateRefused("fee payer starting balance was not observed")
    # The runner installs the baseline ELF at the target address itself.
    needed = tracked - runtime - {event["target"]}
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
        accounts=present, clocks=window_clocks(event, bundle, epoch, scan, from_parent), records=records,
        final_accounts=late["finals"], features=active_features(anchor),
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
