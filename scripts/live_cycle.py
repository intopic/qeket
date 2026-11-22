"""Run the controlled Devnet counter cycle on a GitHub-hosted Linux runner."""
import argparse
import hashlib
import json
import os
import struct
import subprocess
import time
from pathlib import Path

from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import create_account, transfer
from solders.transaction import Transaction
from live_capture import FINAL, GENESIS, proposals, rpc
from devnet import LOADER
import live_capture
import base64

ROOT = Path.cwd()
OUT = ROOT / "target/live"
PRIVATE = Path(os.environ["RUNNER_TEMP"]) / "devnet-keys"
loader = Pubkey.from_string(LOADER)
system = Pubkey.default()
clock_id = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
pack = struct.pack


def key(name):
    return Keypair.from_bytes(bytes(json.loads((PRIVATE / name).read_text())))


def prepare():
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    for name, secret in [("payer", "DEVNET_PAYER"), ("gate", "GATE_PROGRAM_KEYPAIR")]:
        if name == "gate" and not os.environ.get(secret):
            kp = Keypair.from_seed(hashlib.sha256(bytes(key("payer").sign_message(f"canary-devnet-single-verifier:{os.environ['GITHUB_RUN_ID']}".encode()))).digest())
        else:
            kp = Keypair.from_bytes(bytes(json.loads(os.environ[secret])))
        (PRIVATE / name).write_text(kp.to_json())
    for name in ["target", "buffer", "counter", "traffic", "emergency", "vote0"]:
        seed = hashlib.sha256(bytes(key("payer").sign_message(
            f"canary:devnet:{os.environ['GITHUB_RUN_ID']}:{name}".encode()))).digest()
        (PRIVATE / name).write_text(Keypair.from_seed(seed).to_json())
    # Both binaries must agree on the deployment address.
    lib = ROOT / "programs/canary_upgrade_gate/src/lib.rs"
    lib.write_text(lib.read_text().replace(live_capture.GATE, str(key("gate").pubkey())))


def finalized(sig):
    for _ in range(140):
        status = rpc("getSignatureStatuses", [sig], {"searchTransactionHistory": True})["value"][0]
        if status:
            if status["err"]:
                raise RuntimeError(f"transaction {sig} failed: {status['err']}")
            if status["confirmationStatus"] == "finalized":
                return
        time.sleep(1)
    raise TimeoutError(sig)


def receipt(label, sig):
    finalized(sig)
    url = f"https://explorer.solana.com/tx/{sig}?cluster=devnet" if os.environ.get("GITHUB_EVENT_NAME") != "push" else sig
    with (OUT / "transactions.md").open("a") as f:
        print(f"- [{label}]({url})", file=f)
    return sig


def send(label, ix, fee=None, extra=()):
    fee = fee or key("payer")
    bh = Hash.from_string(rpc("getLatestBlockhash", FINAL)["value"]["blockhash"])
    tx = Transaction.new_signed_with_payer(ix, fee.pubkey(), [fee, *extra], bh)
    sig = rpc("sendTransaction", base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "preflightCommitment": "finalized"})
    return receipt(label, sig)


def proposal_state(data):
    return dict(diff_root=data[189:221], count=data[222], ready_at=struct.unpack_from("<q", data, 239)[0],
                status=data[247])


def run():
    rehearsal = os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("RPC_URL") == "http://127.0.0.1:8899"
    if not rehearsal and rpc("getGenesisHash") != GENESIS:
        raise ValueError("expected Devnet")
    OUT.mkdir(parents=True, exist_ok=True)
    gate, target, buf, payer, counter, traffic = [key(n).pubkey() for n in ("gate","target","buffer","payer","counter","traffic")]
    live_capture.GATE = str(gate)
    (OUT / "gate-program-id.txt").write_text(str(gate) + "\n", encoding="utf-8")
    print(f"Gate program ID: {gate}", flush=True)
    def pda(*seeds, owner=gate):
        return Pubkey.find_program_address(list(map(bytes, seeds)), owner)[0]
    def ix(name, metas, data=b""):
        return Instruction(gate, hashlib.sha256(("global:"+name).encode()).digest()[:8]+data, metas)
    def meta(k, writable=False, signer=False):
        return AccountMeta(k, signer, writable)
    def get(k):
        return rpc("getAccountInfo", str(k), dict(FINAL, encoding="base64"))["value"]
    def cli(label, args):
        cmd = ["solana", "--url", os.environ["RPC_URL"], "--keypair", str(PRIVATE/"payer"), "--output", "json", *map(str,args)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(label + ": " + result.stderr.replace(os.environ["RPC_URL"], "[RPC]")[-1200:])
        return json.loads(result.stdout)
    base, candidate = [ROOT/f"target/mvp/{d}/sbpf-solana-solana/release/canary_counter.so" for d in ("baseline","candidate")]
    gate_so = ROOT/"target/sbpf-solana-solana/release/canary_upgrade_gate.so"
    for name, address, binary in [("gate",gate,gate_so),("target",target,base)]:
        if not get(address):
            capacity = max(base.stat().st_size, candidate.stat().st_size) if name == "target" else binary.stat().st_size
            cli("deploy "+name, ["program","deploy",binary,"--max-len",str(capacity),"--program-id",PRIVATE/name,"--use-rpc"])
            for _ in range(90):
                if get(address):
                    break
                time.sleep(1)
        pd = get(pda(address, owner=loader))
        body = base64.b64decode(pd["data"][0])[45:]
        if not body.startswith(binary.read_bytes()) or any(body[binary.stat().st_size:]):
            raise ValueError("deployed ELF mismatch: "+name)
        sigs = rpc("getSignaturesForAddress", str(address), dict(FINAL, limit=1))
        if sigs:
            receipt("program verified: "+name, sigs[0]["signature"])
    cli("write buffer", ["program","write-buffer",candidate,"--buffer",PRIVATE/"buffer","--use-rpc"])
    for _ in range(90):
        if get(buf):
            break
        time.sleep(1)
    cfg, pd = pda(b"gatekeeper_cfg",target), pda(target,owner=loader)
    verifier = key("vote0").pubkey()
    sample_count = int(os.getenv("CANARY_SAMPLE_COUNT", "50"))
    if not 50 <= sample_count <= 100:
        raise ValueError("CANARY_SAMPLE_COUNT must be between 50 and 100")
    span = 256
    policy = bytes(1)+bytes(verifier)+bytes(192)+pack("<BBHIQqq",1,1,1500,sample_count,span,5,3600)
    send("initialize_gatekeeper",[ix("initialize_gatekeeper",[meta(payer,True,True),meta(payer,False,True),meta(target),meta(pd),meta(cfg,True),meta(system)],policy+bytes(key("emergency").pubkey()))])
    send("transfer upgrade and buffer authorities",[Instruction(loader,pack("<I",4),[meta(k,True),meta(payer,False,True),meta(cfg)]) for k in [pd,buf]])
    rent = rpc("getMinimumBalanceForRentExemption",8)
    send("prepare counter, replay payer and verifier", [
        create_account(dict(from_pubkey=payer, to_pubkey=counter, lamports=rent, space=8, owner=target)),
        transfer(dict(from_pubkey=payer, to_pubkey=traffic, lamports=2_000_000)),
        transfer(dict(from_pubkey=payer, to_pubkey=verifier, lamports=20_000_000)),
    ], extra=[key("counter")])
    proposal, reservation = pda(b"proposal",target,bytes(8)), pda(b"buffer",cfg,buf)
    sha = hashlib.sha256(candidate.read_bytes()).digest()
    commit = os.environ["GITHUB_SHA"]
    sig = send("propose_buffer_upgrade",[ix("propose_buffer_upgrade",[meta(payer,True,True),meta(payer,False,True),meta(cfg,True),meta(pd),meta(buf),meta(proposal,True),meta(reservation,True),meta(system)],bytes(payer)+sha+bytes.fromhex(commit)+bytes(1)+pack("<Q",span))])
    logs = rpc("getTransaction",sig,dict(FINAL,encoding="json",maxSupportedTransactionVersion=1))["meta"]["logMessages"]
    event, = proposals(logs)
    if event["proposal"] != str(proposal) or event["target"] != str(target) or event["replay_slots"] != span:
        raise ValueError("unexpected proposal event")
    start = event["anchor_slot"]
    # Leave the worker time to capture the pre-state before changing the counter.
    deadline = time.monotonic() + 120
    while rpc("getSlot", FINAL) <= start + 32:
        if time.monotonic() >= deadline:
            raise TimeoutError("anchor capture window did not close")
        time.sleep(1)
    traffic_key = key("traffic")
    bh = Hash.from_string(rpc("getLatestBlockhash", FINAL)["value"]["blockhash"])
    sample_signatures = []
    for increment in range(1, sample_count + 1):
        instructions = [set_compute_unit_limit(50_000),
                        Instruction(target, pack("<Q", increment), [meta(counter, True)])]
        tx = Transaction.new_signed_with_payer(instructions, traffic, [traffic_key], bh)
        sig = rpc("sendTransaction", base64.b64encode(bytes(tx)).decode(),
                  {"encoding": "base64", "preflightCommitment": "finalized"})
        sample_signatures.append(sig)
    for sig in sample_signatures:
        receipt("counter replay sample", sig)
    deadline = time.monotonic() + 300
    while rpc("getSlot", FINAL) <= start + span:
        if time.monotonic() >= deadline:
            raise TimeoutError("replay window did not finalize")
        time.sleep(2)
    deadline = time.monotonic() + 2400
    while (state := proposal_state(base64.b64decode(get(proposal)["data"][0])))["status"] != 1:
        if state["status"] != 0 or time.monotonic() >= deadline:
            raise RuntimeError(f"verifier did not approve the proposal: {state}")
        time.sleep(5)
    if state["count"] != 1:
        raise ValueError("expected exactly one finalized verifier vote")
    history = rpc("getSignaturesForAddress", str(proposal), dict(FINAL, limit=20))
    for entry in reversed(history):
        if entry["err"] is not None:
            continue
        logs = rpc("getTransaction", entry["signature"], dict(FINAL, encoding="json", maxSupportedTransactionVersion=1))["meta"]["logMessages"]
        if any("Instruction: SubmitVerifiedAttestation" in line for line in logs):
            receipt("worker vote (submit_verified_attestation)", entry["signature"])
    with (OUT / "transactions.md").open("a") as report:
        print(f"- single verifier diff_root `{state['diff_root'].hex()}`", file=report)
    ready_at = struct.unpack_from("<q",base64.b64decode(get(proposal)["data"][0]),-9)[0]
    deadline = time.monotonic() + 120
    while struct.unpack("<QqQQq",base64.b64decode(get(clock_id)["data"][0]))[-1] < ready_at + 5:
        if time.monotonic() >= deadline:
            raise TimeoutError("finalized clock did not reach proposal timelock")
        time.sleep(1)
    send("execute_gated_upgrade",[set_compute_unit_limit(1_400_000),ix("execute_gated_upgrade",[meta(cfg),meta(proposal,True),meta(target,True),meta(pd,True),meta(buf,True),meta(payer,True),meta(Pubkey.from_string("SysvarRent111111111111111111111111111111111")),meta(clock_id),meta(loader)])])
    body=base64.b64decode(get(pd)["data"][0])[45:]
    if not body.startswith(candidate.read_bytes()) or any(body[candidate.stat().st_size:]):
        raise ValueError("final ProgramData mismatch")


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["prepare","run"])
    prepare() if parser.parse_args().action == "prepare" else run()
