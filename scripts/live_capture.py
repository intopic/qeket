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
from pathlib import Path

from devnet import GATE, GENESIS, from_base58, to_base58

GATE = os.environ.get("GATE_PROGRAM_ID", GATE)
EVENT = hashlib.sha256(b"event:ProposalCreated").digest()[:8]
UPGRADE = hashlib.sha256(b"event:UpgradeExecuted").digest()[:8]
EMERGENCY = hashlib.sha256(b"event:EmergencyOverrideTriggered").digest()[:8]
FINAL = {"commitment": "finalized"}


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



def proposals(logs):
    for name, payload in gate_events(logs):
        if name == "ProposalCreated":
            yield payload


def print_event(event):
    print(json.dumps(event), flush=True)


def watch(start, on_event=print_event, checkpoint=None, wait=time.sleep, on_lifecycle=None):
    cursor = start if start is not None else rpc("getSlot", FINAL)
    while True:
        tip = rpc("getSlot", FINAL)
        if cursor <= tip:
            before = None
            events = []
            while True:
                opts = dict(FINAL, limit=100)
                if before:
                    opts["before"] = before
                page = rpc("getSignaturesForAddress", GATE, opts)
                for entry in page:
                    if cursor <= entry["slot"] <= tip and entry["err"] is None:
                        events.append(entry)
                        if len(events) > 1000:
                            raise ValueError("proposal history limit exceeded")
                if len(page) < 100 or page[-1]["slot"] < cursor:
                    break
                before = page[-1]["signature"]
            for entry in reversed(events):
                sig = entry["signature"]
                item = rpc("getTransaction",sig,dict(FINAL,encoding="json",maxSupportedTransactionVersion=1))
                if not item or not item.get("meta"):
                    raise RuntimeError("finalized gate transaction unavailable")
                for name, event in gate_events(item["meta"]["logMessages"] or []):
                    event = dict(event, slot=entry["slot"], signature=sig)
                    if name == "ProposalCreated":
                        on_event(event)
                    elif on_lifecycle is not None:
                        on_lifecycle(name, event)
            cursor = tip + 1
            if checkpoint is not None:
                checkpoint(cursor)
        else:
            wait(1)


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
    matches = []
    baseline = {}
    inspected = 0
    while True:
        options = dict(FINAL, limit=min(1000, limit + 1))
        if before:
            options["before"] = before
        page = rpc("getSignaturesForAddress", target, options)
        if not page:
            break
        for entry in page:
            inspected += 1
            if inspected > max(1000, limit * 8):
                raise ValueError("address history scan limit exceeded")
            if start <= entry["slot"] <= start + span and entry["err"] is None:
                sig = entry["signature"]
                options_tx = dict(FINAL, maxSupportedTransactionVersion=1)
                item = rpc("getTransaction", sig, dict(options_tx, encoding="json"))
                if not item or not item.get("meta"):
                    raise RuntimeError("finalized transaction metadata unavailable")
                if item["slot"] != entry["slot"]:
                    raise ValueError("transaction slot mismatch")
                if invoked(item, target):
                    if item.get("version", "legacy") not in ("legacy", 0):
                        raise UnsupportedReplayVersion(
                            f"target transaction at slot {item['slot']} has unsupported version {item['version']}: {sig}"
                        )
                    matches.append((item["slot"], sig))
                    if len(matches) > limit:
                        raise ValueError("max_replay_txs exceeded")
                    if validate_transaction is not None:
                        validate_transaction(item)
                    if include_baseline:
                        baseline[sig] = baseline_observation(item)
        if page[-1]["slot"] < start or len(page) < options["limit"]:
            break
        before = page[-1]["signature"]
    for slot in sorted({slot for slot, _ in matches}):
        signatures = rpc("getBlock", slot, dict(FINAL, transactionDetails="signatures", rewards=False, maxSupportedTransactionVersion=1))["signatures"]
        positions = {sig: i for i, sig in enumerate(signatures)}
        for index, sig in sorted((positions[s], s) for n,s in matches if n == slot):
            tx = rpc("getTransaction", sig, dict(FINAL, encoding="base64", maxSupportedTransactionVersion=1))
            raw = base64.b64decode(tx["transaction"][0], validate=True)
            signature = from_base58(sig)
            if tx["slot"] != slot or len(raw) > 1232 or raw[1:65] != signature:
                raise ValueError("transaction differs from finalized block")
            record = dict(slot=slot, index=index, transaction=raw.hex())
            if include_baseline:
                record["baseline"] = baseline[sig]
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
    if rpc("getGenesisHash") != GENESIS:
        raise ValueError("expected Solana Devnet")
    if args.action == "watch":
        watch(args.start_slot)
    else:
        result = capture(json.loads(args.snapshot.read_bytes()), args.max_replay_txs)
        args.archive.write_text(json.dumps(result), encoding="utf-8")
