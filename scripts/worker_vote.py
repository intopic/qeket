"""Verify a captured window against an independent rebuild, then submit this node's vote."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from live_capture import FINAL, rpc
from live_prestate import baseline_elf
from solana_tx import (INSTRUCTIONS_SYSVAR, Keypair, anchor_discriminator, attestation_payload,
                       ed25519_instruction, transaction)

VOTE_VALIDITY_SLOTS = 300
# `canary-gate verify` exits with this code only when the candidate failed a check.
CANDIDATE_FAULT_EXIT = 3
# Customer state and build output. evidence.json holds only hashes and counts.
JOB_FILES = ("archive.json", "baseline.so", "build")


class CandidateRejected(ValueError):
    """The replay or binary comparison disproved the candidate."""


class ReplayIncomplete(ValueError):
    """The archive or the replay could not establish the evidence. The candidate is not judged."""


class VoteRefused(ValueError):
    """The gate refused the vote. Retrying the same vote cannot succeed."""


def load_keypair(environ=os.environ):
    if environ.get("CANARY_VERIFIER_KEYPAIR"):
        return Keypair.from_json(environ["CANARY_VERIFIER_KEYPAIR"])
    if environ.get("CANARY_VERIFIER_KEYPAIR_FILE"):
        return Keypair.from_json(Path(environ["CANARY_VERIFIER_KEYPAIR_FILE"]).read_text(encoding="utf-8"))
    return None


def write_atomic(path, data):
    staged = path.with_name("." + path.name + ".tmp")
    staged.write_bytes(data)
    staged.replace(path)


def verify_replay(runner, archive_path, baseline_path, rebuilt_path):
    try:
        result = subprocess.run([runner, "verify", str(archive_path), str(baseline_path), str(rebuilt_path)],
                                capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        raise RuntimeError("replay verification timed out") from None
    if result.returncode:
        # anyhow prints the outermost context first. Keep the decisive line.
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        reason = lines[0].removeprefix("Error: ")[:300] if lines else "verification failed"
        if result.returncode == CANDIDATE_FAULT_EXIT:
            raise CandidateRejected(reason)
        raise ReplayIncomplete(reason)
    return json.loads(result.stdout)


def discard_job_files(directory):
    """Remove captured state and build output once the job has an outcome."""
    for name in JOB_FILES:
        path = directory / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def wait_finalized(signature, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = rpc("getSignatureStatuses", [signature], {"searchTransactionHistory": True})["value"][0]
        if status:
            if status["err"]:
                raise VoteRefused(f"vote failed on chain: {status['err']}")
            if status["confirmationStatus"] == "finalized":
                return
        time.sleep(2)
    raise RuntimeError("vote was not finalized in time")


def submit_vote(event, bundle, evidence, keypair, gate):
    slot = rpc("getSlot", FINAL)
    payload = attestation_payload(
        evidence["replay_mode"], event["target"], event["nonce"], bundle["buffer"], event["anchor_slot"],
        event["replay_slots"], evidence["elf_sha256"], evidence["diff_root"], evidence["replayed_tx_count"],
        evidence["max_cu_delta_bps"], slot + VOTE_VALIDITY_SLOTS)
    verify = ed25519_instruction(keypair.public, keypair.sign(payload), payload)
    submit = (gate, [(bundle["config_address"], False, False), (event["proposal"], False, True),
                     (INSTRUCTIONS_SYSVAR, False, False)], anchor_discriminator("submit_verified_attestation"))
    blockhash = rpc("getLatestBlockhash", FINAL)["value"]["blockhash"]
    raw = transaction(keypair.address, [verify, submit], blockhash, [keypair])
    try:
        signature = rpc("sendTransaction", base64.b64encode(raw).decode(),
                        {"encoding": "base64", "preflightCommitment": "finalized"})
    except RuntimeError as exc:
        message = str(exc)
        if "AlreadyVoted" in message:
            return None
        if message.startswith("sendTransaction: -3200"):
            raise VoteRefused(message[:400]) from None
        raise
    wait_finalized(signature)
    return signature


def attest(event, bundle, policy, directory, runner, build, keypair, gate):
    """Return the outcome for one proposal. Transient failures raise RuntimeError or OSError."""
    directory.mkdir(parents=True, exist_ok=True)
    evidence = bundle.get("evidence")
    if evidence is None:
        archive_path, baseline_path = directory / "archive.json", directory / "baseline.so"
        write_atomic(archive_path, json.dumps(bundle["archive"], separators=(",", ":")).encode())
        write_atomic(baseline_path, baseline_elf(bundle))
        rebuilt = build(policy, bundle["source_commit"], directory / "build")
        evidence = verify_replay(runner, archive_path, baseline_path, rebuilt)
        if bytes(evidence["elf_sha256"]).hex() != bundle["candidate_sha256"]:
            raise CandidateRejected("rebuilt ELF differs from the proposed hash")
        if evidence["replay_mode"] != bundle["config"]["replay_mode"]:
            raise ReplayIncomplete("replay mode differs from the gate policy")
        write_atomic(directory / "evidence.json", json.dumps(evidence).encode())
        bundle["evidence"] = evidence
    outcome = dict(diff_root=bytes(evidence["diff_root"]).hex(), replayed_tx_count=evidence["replayed_tx_count"],
                   max_cu_delta_bps=evidence["max_cu_delta_bps"])
    if keypair is None or keypair.address not in bundle["config"]["verifiers"]:
        return dict(outcome, status="verified")
    signature = submit_vote(event, bundle, evidence, keypair, gate)
    return dict(outcome, status="voted", verifier=keypair.address, signature=signature)
