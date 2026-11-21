import base64
import json
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from devnet import from_base58, to_base58
from solana_tx import (INSTRUCTIONS_SYSVAR, Keypair, anchor_discriminator, attestation_payload,
                       ed25519_instruction, find_program_address, public_key, sign, transaction)
import worker_vote
from worker_vote import CandidateRejected, ReplayIncomplete, VoteRefused, attest, verify_replay

SEED = bytes(range(32))
KEYPAIR = Keypair(SEED + public_key(SEED))


class SolanaTransactionTest(unittest.TestCase):
    def test_rfc8032_and_solana_vectors(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(public_key(seed).hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        self.assertEqual(sign(seed, b"").hex(),
                         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
                         "1cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(KEYPAIR.address, "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF")
        with self.assertRaises(ValueError):
            Keypair(SEED + bytes(32))
        self.assertEqual(find_program_address([b"gatekeeper_cfg", bytes([1]) * 32],
                                              "38SJ9rbJgLtibh4bL1VyoGb5uMaEQtSPBJZbTFahYtmz"),
                         ("HKHXwoiYQ5q6xfSh1rkpYYoZv1qswoqEatEzAM4GccXB", 252))
        self.assertEqual(find_program_address([bytes([58]) * 32], "BPFLoaderUpgradeab1e11111111111111111111111"),
                         ("48CUEePo49Jay69UL4nZCPbEB8yXdoa24GbLgkioQ1FB", 252))

    def test_vote_transaction_layout(self):
        payload = attestation_payload("StrictEquality", to_base58(bytes([1]) * 32), 7, to_base58(bytes([2]) * 32),
                                      100, 256, [3] * 32, [4] * 32, 50, 1500, 999)
        self.assertEqual(len(payload), 186)
        self.assertEqual(payload[:16], b"CANARY_GATE_M1__")
        self.assertEqual(struct.unpack_from("<IIHQ", payload, 168), (50, 0, 1500, 999))
        program, metas, data = ed25519_instruction(KEYPAIR.public, KEYPAIR.sign(payload), payload)
        self.assertEqual((metas, data[:16], data[112:]),
                         ([], struct.pack("<BB7H", 1, 0, 48, 0xFFFF, 16, 0xFFFF, 112, 186, 0xFFFF), payload))
        gate, config, proposal = (to_base58(bytes([n]) * 32) for n in (5, 6, 7))
        submit = (gate, [(config, False, False), (proposal, False, True), (INSTRUCTIONS_SYSVAR, False, False)],
                  anchor_discriminator("submit_verified_attestation"))
        raw = transaction(KEYPAIR.address, [(program, metas, data), submit], to_base58(bytes([9]) * 32), [KEYPAIR])
        # The README budget for a compact vote.
        self.assertEqual(len(raw), 610)
        message = raw[65:]
        self.assertEqual(message[:4], bytes([1, 0, 4, 6]))
        keys = [message[4 + 32 * i:36 + 32 * i] for i in range(6)]
        self.assertEqual(keys[:2], [KEYPAIR.public, from_base58(proposal)])
        self.assertEqual(raw[1:65], KEYPAIR.sign(message))
        with self.assertRaises(ValueError):
            transaction(KEYPAIR.address, [submit], to_base58(bytes(32)), [])

    def test_matches_solders_when_installed(self):
        try:
            from solders.transaction import Transaction
        except ImportError:
            self.skipTest("solders is not installed")
        payload = os.urandom(186)
        ed = ed25519_instruction(KEYPAIR.public, KEYPAIR.sign(payload), payload)
        raw = transaction(KEYPAIR.address, [ed], to_base58(bytes([9]) * 32), [KEYPAIR])
        self.assertEqual(bytes(Transaction.from_bytes(raw)), raw)
        self.assertTrue(Transaction.from_bytes(raw).verify() is None)


class AttestationTest(unittest.TestCase):
    def bundle(self, verifiers):
        return dict(archive={"records": []}, source_commit="ab" * 20, candidate_sha256="11" * 32, buffer="b",
                    config=dict(replay_mode="StrictEquality", verifiers=verifiers), config_address="c",
                    program_data="pd", accounts={"pd": dict(owner="BPFLoaderUpgradeab1e11111111111111111111111",
                                                            data=[base64.b64encode(b"h" * 45 + b"ELF").decode()])})

    def test_verifies_rebuild_then_votes_once(self):
        evidence = dict(replay_mode="StrictEquality", elf_sha256=[0x11] * 32, diff_root=[2] * 32,
                        replayed_tx_count=50, max_cu_delta_bps=0)
        with TemporaryDirectory() as tmp:
            build = unittest.mock.Mock(return_value=Path(tmp) / "rebuilt.so")
            bundle = self.bundle([KEYPAIR.address])
            with patch("worker_vote.verify_replay", return_value=evidence) as replay, patch(
                    "worker_vote.submit_vote", return_value="sig") as vote:
                result = attest({}, bundle, None, Path(tmp) / "job", "canary-gate", build, KEYPAIR, "gate")
                self.assertEqual((result["status"], result["signature"], result["diff_root"]), ("voted", "sig", "02" * 32))
                self.assertEqual((Path(tmp) / "job/baseline.so").read_bytes(), b"ELF")
                self.assertEqual(json.loads((Path(tmp) / "job/archive.json").read_text()), {"records": []})
                # A transient vote failure must not rebuild.
                attest({}, bundle, None, Path(tmp) / "job", "canary-gate", build, KEYPAIR, "gate")
                self.assertEqual((build.call_count, replay.call_count, vote.call_count), (1, 1, 2))
            observer = attest({}, self.bundle(["other"]) | {"evidence": evidence}, None, Path(tmp) / "o",
                              "canary-gate", build, KEYPAIR, "gate")
            self.assertEqual(observer["status"], "verified")
            with patch("worker_vote.verify_replay", return_value=dict(evidence, elf_sha256=[0] * 32)):
                with self.assertRaisesRegex(CandidateRejected, "proposed hash"):
                    attest({}, self.bundle([]), None, Path(tmp) / "r", "canary-gate", build, None, "gate")

    def test_only_candidate_faults_reject_the_candidate(self):
        def runner(tmp, code, message):
            script = f"import sys; sys.stderr.write('Error: {message}\\n\\nCaused by: y\\n'); sys.exit({code})"
            path = Path(tmp) / (f"runner{code}.cmd" if os.name == "nt" else f"runner{code}")
            path.write_text(f"@\"{sys.executable}\" -c \"{script}\"\n" if os.name == "nt"
                            else f"#!/bin/sh\nexec '{sys.executable}' -c \"{script}\"\n")
            path.chmod(0o755)
            return str(path)

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CandidateRejected, "^post-state differs for X$"):
                verify_replay(runner(tmp, 3, "post-state differs for X"), "a", "b", "c")
            # A failing baseline or missing state says nothing about the candidate.
            with self.assertRaisesRegex(ReplayIncomplete, "^baseline failed at 1:0$") as caught:
                verify_replay(runner(tmp, 1, "baseline failed at 1:0"), "a", "b", "c")
            self.assertNotIsInstance(caught.exception, CandidateRejected)

    def test_job_files_are_discarded_but_evidence_stays(self):
        from worker_vote import discard_job_files
        with TemporaryDirectory() as tmp:
            job = Path(tmp)
            for name in ("archive.json", "baseline.so", "evidence.json", "build/candidate/x.so"):
                (job / name).parent.mkdir(parents=True, exist_ok=True)
                (job / name).write_text("x")
            discard_job_files(job)
            self.assertEqual(sorted(p.name for p in job.iterdir()), ["evidence.json"])

    def test_gate_refusals_are_final(self):
        event = dict(target=to_base58(bytes([1]) * 32), nonce=0, anchor_slot=1, replay_slots=2,
                     proposal=to_base58(bytes([2]) * 32))
        bundle = dict(buffer=to_base58(bytes([3]) * 32), config_address=to_base58(bytes([4]) * 32))
        evidence = dict(replay_mode="StrictEquality", elf_sha256=[0] * 32, diff_root=[0] * 32,
                        replayed_tx_count=1, max_cu_delta_bps=0)

        def reply(error):
            def rpc(method, *params):
                if method == "sendTransaction":
                    raise RuntimeError(error)
                return {"getSlot": 10, "getLatestBlockhash": {"value": {"blockhash": to_base58(bytes(32))}}}[method]
            return rpc

        with patch("worker_vote.rpc", reply("sendTransaction: -32002 failed, logs=['Error Code: AlreadyVoted']")):
            self.assertIsNone(worker_vote.submit_vote(event, bundle, evidence, KEYPAIR, to_base58(bytes([5]) * 32)))
        with patch("worker_vote.rpc", reply("sendTransaction: -32002 failed, logs=['Error Code: Expired']")):
            with self.assertRaisesRegex(VoteRefused, "Expired"):
                worker_vote.submit_vote(event, bundle, evidence, KEYPAIR, to_base58(bytes([5]) * 32))
        with patch("worker_vote.rpc", reply("sendTransaction: HTTP 503")):
            with self.assertRaisesRegex(RuntimeError, "503"):
                worker_vote.submit_vote(event, bundle, evidence, KEYPAIR, to_base58(bytes([5]) * 32))


class BuilderTest(unittest.TestCase):
    def test_build_tree_excludes_local_overrides_and_links(self):
        from worker_build import BuildFailed, check_options, copy_source, fetch_commit
        with TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "src", Path(tmp) / "out"
            for name in ("Cargo.toml", "Cargo.lock", "src/lib.rs", "target/x", ".cargo/config.toml",
                         "rust-toolchain.toml", "nested/target/kept"):
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_text(name)
            copy_source(source, out)
            copied = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
            self.assertEqual(copied, ["Cargo.lock", "Cargo.toml", "nested/target/kept", "src/lib.rs"])
            try:
                (source / "src/link").symlink_to(Path(tmp) / "elsewhere")
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(BuildFailed, "symbolic link"):
                    copy_source(source, Path(tmp) / "again")
            for subdir, library, features in (("../x", "p", []), ("a", "", []), ("a", "p", ["--all"])):
                with self.subTest(subdir=subdir, library=library), self.assertRaises(BuildFailed):
                    check_options(subdir, library, features)
            for url, commit in (("https://github.com/a/b", "HEAD"), ("file:///tmp/x", "a" * 40)):
                with self.assertRaises(BuildFailed):
                    fetch_commit(url, commit, Path(tmp) / "checkout")

    def test_fly_image_uses_unpacked_platform_tools(self):
        from worker_build import platform_root
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"CANARY_PLATFORM_TOOLS": tmp}):
            self.assertEqual(platform_root("unused-toolchain"), Path(tmp).resolve())

    def test_keygen_prints_secret_on_stdout_only(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("solana_tx.py")), "keygen"],
                                capture_output=True, text=True, check=True)
        keypair = Keypair(json.loads(result.stdout))
        self.assertEqual(result.stderr.strip(), keypair.address)
        self.assertNotIn("[", result.stderr)


if __name__ == "__main__":
    unittest.main()
