"""Stream finalized Devnet proposal events and capture signed target transactions."""
import argparse
import base64
import hashlib
import json
import os
import re
import struct
import time
import urllib.request
import urllib.error
from http.client import HTTPException
from pathlib import Path

# GENESIS is Devnet's. live_cycle, the Devnet rehearsal, imports it from here.
from devnet import GATE, GENESIS, check_cluster, from_base58, to_base58

GATE = os.environ.get("GATE_PROGRAM_ID", GATE)
EVENT = hashlib.sha256(b"event:ProposalCreated").digest()[:8]
UPGRADE = hashlib.sha256(b"event:UpgradeExecuted").digest()[:8]
EMERGENCY = hashlib.sha256(b"event:EmergencyOverrideTriggered").digest()[:8]
CANCELLED = hashlib.sha256(b"event:ProposalCancelled").digest()[:8]
POLICY_UPDATED = hashlib.sha256(b"event:PolicyUpdated").digest()[:8]
FINAL = {"commitment": "finalized"}
# Longest wait between reads while the endpoint is throttled or unreachable.
MAX_RPC_BACKOFF = 60


def rpc(method, *params):
    request = urllib.request.Request(os.environ["RPC_URL"], json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }).encode(), {"Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"{method}: HTTP {exc.code}") from None
        except (TimeoutError, urllib.error.URLError):
            if attempt == 4:
                raise RuntimeError(f"{method}: endpoint unavailable") from None
        time.sleep(2 ** attempt)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("RPC response exceeds capture limit")
    result = json.loads(data)
    if "error" in result:
        err = result["error"]
        logs = (err.get("data") or {}).get("logs") if isinstance(err.get("data"), dict) else None
        raise RuntimeError(f"{method}: {err.get('code')} {err.get('message')}, logs={logs}")
    return result["result"]


def block(slot, encoding):
    return rpc("getBlock", slot, dict(FINAL, encoding=encoding,
               transactionDetails="full", rewards=False, maxSupportedTransactionVersion=1))


def gate_events(logs):
    stack = []
    for line in logs:
        invocation = re.fullmatch(r"Program (\w+) invoke \[(\d+)\]", line)
        if invocation:
            stack.append(invocation[1])
        elif re.fullmatch(r"Program \w+ (success|failed: .*)", line):
            if stack:
                stack.pop()
        elif stack and stack[-1] == GATE and line.startswith("Program data: "):
            raw = base64.b64decode(line[14:], validate=True)
            if len(raw) == 96 and raw.startswith(EVENT):
                nonce, anchor, span = struct.unpack_from("<QQQ", raw, 72)
                yield "ProposalCreated", dict(proposal=to_base58(raw[8:40]), target=to_base58(raw[40:72]),
                                              nonce=nonce, anchor_slot=anchor, replay_slots=span)
            elif len(raw) == 72 and raw.startswith(UPGRADE):
                yield "UpgradeExecuted", dict(proposal=to_base58(raw[8:40]), target=to_base58(raw[40:72]))
            elif len(raw) == 104 and raw.startswith(EMERGENCY):
                yield "EmergencyOverrideTriggered", dict(
                    target=to_base58(raw[8:40]), buffer=to_base58(raw[40:72]), authority=to_base58(raw[72:104]))
            elif len(raw) == 40 and raw.startswith(CANCELLED):
                yield "ProposalCancelled", dict(proposal=to_base58(raw[8:40]))
            elif len(raw) == 48 and raw.startswith(POLICY_UPDATED):
                # Applying a policy starts a new proposal generation at next_nonce.
                yield "PolicyUpdated", dict(config=to_base58(raw[8:40]),
                                            next_nonce=struct.unpack_from("<Q", raw, 40)[0])



def proposals(logs):
    for name, payload in gate_events(logs):
        if name == "ProposalCreated":
            yield payload


def print_event(event):
    print(json.dumps(event), flush=True)


class NodeBehind(RuntimeError):
    """The RPC node that answered has not reached the slot the request needs."""


class HistoryLimit(ValueError):
    pass


def behind(error):
    return isinstance(error, NodeBehind) or "-32016" in str(error)


def unavailable(error):
    """The endpoint throttled or dropped the request after rpc() retried it. A later read can succeed."""
    if isinstance(error, (OSError, HTTPException)):
        return True
    return re.search(r": (HTTP (429|5\d\d)|endpoint unavailable)$", str(error)) is not None


def gate_history(cursor, tip):
    """Successful gate transactions in [cursor, tip], oldest first, from a node at or past tip."""
    before = None
    events = []
    while True:
        # A load-balanced endpoint may route this call to a node that has not
        # indexed `tip` yet. Without the floor, the cursor would skip its events.
        opts = dict(FINAL, limit=100, minContextSlot=tip)
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", GATE, opts)
        for entry in page:
            if cursor <= entry["slot"] <= tip and entry["err"] is None:
                events.append(entry)
                if len(events) > 1000:
                    raise HistoryLimit("proposal history limit exceeded")
        if len(page) < 100 or page[-1]["slot"] < cursor:
            break
        before = page[-1]["signature"]
    items = []
    for entry in reversed(events):
        item = rpc("getTransaction", entry["signature"],
                   dict(FINAL, encoding="json", maxSupportedTransactionVersion=1, minContextSlot=tip))
        if not item or not item.get("meta"):
            raise NodeBehind("finalized gate transaction unavailable")
        items.append((entry, item))
    return items


def slot_history(slot):
    """Successful transactions of one finalized slot, in block order.

    Used when a single slot holds more gate transactions than gate_history reads.
    gate_events only reports lines logged by the gate, so other programs in the
    block are ignored.
    """
    try:
        confirmed = block(slot, "json")
    except RuntimeError as exc:
        # -32004 "Block not available for slot": this node has not stored it yet.
        if "-32004" in str(exc):
            raise NodeBehind(str(exc)) from None
        raise
    if not confirmed:
        raise NodeBehind("finalized gate block unavailable")
    return [(dict(slot=slot, signature=item["transaction"]["signatures"][0]), item)
            for item in confirmed["transactions"] if item.get("meta") and item["meta"]["err"] is None]


def watch(start, on_event=print_event, checkpoint=None, wait=time.sleep, on_lifecycle=None):
    cursor = start if start is not None else rpc("getSlot", FINAL)
    span = 64
    outages = 0
    while True:
        try:
            tip = min(rpc("getSlot", FINAL), cursor + span - 1)
            if cursor > tip:
                wait(1)
                continue
            try:
                items = gate_history(cursor, tip)
            except HistoryLimit:
                if cursor < tip:
                    span = max(1, (tip - cursor + 1) // 2)
                    continue
                # One busy slot cannot be split. Its block read is retried
                # like any other history read.
                items = slot_history(cursor)
        except (RuntimeError, OSError, HTTPException) as exc:
            # Retry the same range. The cursor moves only after a complete read.
            if behind(exc):
                wait(1)
                continue
            if not unavailable(exc):
                raise
            # A throttled or unreachable endpoint must not stop the listener:
            # a restart would drop every captured pre-state.
            outages += 1
            delay = min(MAX_RPC_BACKOFF, 2 ** min(outages, 6))
            print(json.dumps(dict(event="rpc_unavailable", error=str(exc)[:200], retry_in=delay)), flush=True)
            wait(delay)
            continue
        outages = 0
        for entry, item in items:
            for name, event in gate_events(item["meta"]["logMessages"] or []):
                event = dict(event, slot=entry["slot"], signature=entry["signature"])
                if name == "ProposalCreated":
                    on_event(event)
                elif on_lifecycle is not None:
                    on_lifecycle(name, event)
        cursor = tip + 1
        span = min(span * 2, 64)
        if checkpoint is not None:
            checkpoint(cursor)


def invoked(item, target):
    meta = item["meta"]
    if not meta or meta["err"] is not None:
        return False
    msg = item["transaction"]["message"]
    loaded = meta.get("loadedAddresses", {})
    keys = msg["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    instructions = list(msg["instructions"])
    if meta.get("innerInstructions") is None and target in keys:
        raise ValueError("missing CPI trace for possible target invocation")
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group["instructions"])
    return any(keys[ix["programIdIndex"]] == target for ix in instructions)


class UnsupportedReplayVersion(ValueError):
    pass


def baseline_observation(item):
    meta = item["meta"]
    message = item["transaction"]["message"]
    loaded = meta.get("loadedAddresses") or {}
    keys = message["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    before, after = meta["preBalances"], meta["postBalances"]
    units = meta.get("computeUnitsConsumed")
    fee = meta.get("fee")
    if (len(before) != len(keys) or len(after) != len(keys) or type(units) is not int or units <= 0
            or type(fee) is not int or fee < 0):
        raise ValueError("incomplete finalized baseline observations")
    return dict(keys=[list(from_base58(key)) for key in keys], pre_balances=before,
                post_balances=after, compute_units=units, fee=fee)


def capture(snapshot, limit, validate_transaction=None, include_baseline=False):
    start, span = snapshot["anchor_slot"], snapshot["replay_slots"]
    if not 0 < span <= 2048 or limit <= 0 or rpc("getSlot", FINAL) < start + span:
        raise ValueError("invalid or unfinalized replay window")
    target = to_base58(bytes(snapshot["target"]))
    records = []
    digest = hashlib.sha256(b"CANARY_TX_LIST_V1")
    before = None
    selected = {}
    inspected = 0
    while True:
        options = dict(FINAL, limit=min(1000, limit + 1), minContextSlot=start + span)
        if before:
            options["before"] = before
        page = rpc("getSignaturesForAddress", target, options)
        if not page:
            break
        for entry in page:
            inspected += 1
            if inspected > max(1000, limit * 8):
                raise ValueError("address history scan limit exceeded")
            slot = entry["slot"]
            if start <= slot <= start + span and entry["err"] is None:
                selected.setdefault(slot, set()).add(entry["signature"])
        if page[-1]["slot"] < start or len(page) < options["limit"]:
            break
        next_before = page[-1]["signature"]
        if before == next_before:
            raise RuntimeError("address history pagination did not advance")
        before = next_before
    for slot, signatures in sorted(selected.items()):
        decoded = block(slot, "json")
        if not decoded:
            raise RuntimeError("finalized transaction metadata unavailable")
        matches, seen = [], set()
        for index, item in enumerate(decoded["transactions"]):
            sig = item["transaction"]["signatures"][0]
            if sig not in signatures:
                continue
            seen.add(sig)
            if not item.get("meta") or item["meta"]["err"] is not None:
                raise RuntimeError("finalized block metadata differs from address history")
            if not invoked(item, target):
                continue
            if item.get("version", "legacy") not in ("legacy", 0):
                raise UnsupportedReplayVersion(
                    f"target transaction at slot {slot} has unsupported version {item['version']}: {sig}"
                )
            matches.append((index, sig, item))
            if len(records) + len(matches) > limit:
                raise ValueError("max_replay_txs exceeded")
        if seen != signatures:
            raise RuntimeError("address history signature missing from finalized block")
        if not matches:
            continue
        encoded = block(slot, "base64")
        if not encoded or encoded["blockhash"] != decoded["blockhash"]:
            raise RuntimeError("RPC encodings differ on the finalized block")
        transactions = encoded["transactions"]
        if len(transactions) != len(decoded["transactions"]):
            raise RuntimeError("RPC encodings differ on the finalized transaction count")
        for index, sig, item in matches:
            raw = base64.b64decode(transactions[index]["transaction"][0], validate=True)
            signature = from_base58(sig)
            if len(raw) > 1232 or raw[1:65] != signature:
                raise ValueError("transaction differs from finalized block")
            record = dict(slot=slot, index=index, transaction=raw.hex())
            if validate_transaction is not None:
                validate_transaction(dict(item, slot=slot))
            if include_baseline:
                record["baseline"] = baseline_observation(item)
            records.append(record)
            digest.update(struct.pack("<QI", slot, index) + signature)
    return dict(snapshot, format_version=2, records=records, transaction_manifest=list(digest.digest()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    listener = sub.add_parser("watch")
    listener.add_argument("--start-slot", type=int)
    reader = sub.add_parser("capture")
    reader.add_argument("snapshot", type=Path)
    reader.add_argument("archive", type=Path)
    reader.add_argument("--max-replay-txs", type=int, default=500)
    args = parser.parse_args()
    check_cluster(rpc("getGenesisHash"))
    if args.action == "watch":
        watch(args.start_slot)
    else:
        result = capture(json.loads(args.snapshot.read_bytes()), args.max_replay_txs)
        args.archive.write_text(json.dumps(result), encoding="utf-8")
