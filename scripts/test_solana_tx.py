import base64
import io
import json
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

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

    def test_pending_vote_rebroadcasts_until_confirmed(self):
        clock = [0]
        confirmed = {"confirmationStatus": "confirmed", "err": None}
        finalized = {"confirmationStatus": "finalized", "err": None}
        statuses = [None, None, None, confirmed, confirmed, finalized]
        rebroadcast = Mock()
        with patch("worker_vote.time.monotonic", side_effect=lambda: clock[0]), patch(
                "worker_vote.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch(
                "worker_vote.rpc", side_effect=[{"value": [value]} for value in statuses]):
            worker_vote.wait_finalized("signature", rebroadcast=rebroadcast)
        rebroadcast.assert_called_once_with()

    def test_gate_refusals_are_final(self):
        event = dict(target=to_base58(bytes([1]) * 32), nonce=0, anchor_slot=1, replay_slots=2,
                     proposal=to_base58(bytes([2]) * 32))
        gate = to_base58(bytes([5]) * 32)
        bundle = dict(buffer=to_base58(bytes([3]) * 32), config_address=to_base58(bytes([4]) * 32),
                      config={"verifiers": [KEYPAIR.address]})
        evidence = dict(replay_mode="StrictEquality", elf_sha256=[0] * 32, diff_root=[0] * 32,
                        replayed_tx_count=1, max_cu_delta_bps=0)

        def reply(error):
            reads = 0
            def rpc(method, *params):
                nonlocal reads
                if method == "getAccountInfo":
                    data = bytearray(248)
                    data[:8] = worker_vote.PROPOSAL_TAG
                    data[17:49] = from_base58(event["target"])
                    data[57:89] = from_base58(bundle["buffer"])
                    data[221] = int(reads > 0 and "AlreadyVoted" in error)
                    reads += 1
                    return {"value": {"owner": gate, "data": [base64.b64encode(data).decode(), "base64"]}}
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


    def test_vote_priority_fee_is_validated_once_and_empty_means_none(self):
        self.assertEqual(worker_vote.vote_priority_fee({}), 0)
        self.assertEqual(worker_vote.vote_priority_fee({"CANARY_VOTE_PRIORITY_FEE": ""}), 0)
        self.assertEqual(worker_vote.vote_priority_fee({"CANARY_VOTE_PRIORITY_FEE": " 1200 "}), 1200)
        self.assertEqual(worker_vote.vote_priority_fee({"CANARY_VOTE_PRIORITY_FEE": str(2**64 - 1)}), 2**64 - 1)
        for value in ("-1", "1.5", "abc", "1e3", str(2**64)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "micro-lamports"):
                worker_vote.vote_priority_fee({"CANARY_VOTE_PRIORITY_FEE": value})
        # fly_listener reads it at startup, before any proposal can be refused over it.
        source = Path(__file__).with_name("fly_listener.py").read_text(encoding="utf-8")
        self.assertLess(source.index("vote_priority_fee()"), source.index("attestor = Attestor("))

    def test_vote_intent_is_written_after_authorization_and_before_sending(self):
        event = dict(target=to_base58(bytes([1]) * 32), nonce=0, anchor_slot=1, replay_slots=2,
                     proposal=to_base58(bytes([2]) * 32))
        gate = to_base58(bytes([5]) * 32)
        bundle = dict(buffer=to_base58(bytes([3]) * 32), config_address=to_base58(bytes([4]) * 32),
                      config={"verifiers": [KEYPAIR.address]})
        evidence = dict(replay_mode="StrictEquality", elf_sha256=[0] * 32, diff_root=[0] * 32,
                        replayed_tx_count=1, max_cu_delta_bps=0)
        sent = []
        with TemporaryDirectory() as tmp:
            job = Path(tmp)

            def rpc(method, *params):
                if method == "getAccountInfo":
                    return {"value": None}  # No finalized vote yet.
                if method == "getSlot":
                    return 10
                if method == "getLatestBlockhash":
                    return {"value": {"blockhash": to_base58(bytes(32)), "lastValidBlockHeight": 77}}
                if method == "sendTransaction":
                    sent.append((json.loads((job / "vote-intent.json").read_text()), base64.b64decode(params[0])))
                    return "sig"
                raise AssertionError(method)

            def refuse():
                raise VoteRefused("proposal or customer authorization expired or changed")

            with patch("worker_vote.rpc", side_effect=rpc), patch("worker_vote.wait_finalized") as finalized, patch(
                    "worker_vote.transaction", wraps=transaction) as signed, patch.dict(
                    os.environ, {"CANARY_VOTE_PRIORITY_FEE": "1200"}):
                with self.assertRaisesRegex(VoteRefused, "authorization"):
                    worker_vote.submit_vote(event, bundle, evidence, KEYPAIR, gate, refuse, job)
                # Nothing was signed, recorded or sent.
                self.assertEqual((sent, list(job.iterdir())), ([], []))
                self.assertEqual(worker_vote.submit_vote(event, bundle, evidence, KEYPAIR, gate, lambda: None, job),
                                 "sig")
                finalized.assert_called_once_with("sig", rebroadcast=ANY)
                finalized.call_args.kwargs["rebroadcast"]()
            signed.assert_called_once()
            limit, price, verify, submit = signed.call_args.args[1]
            # The fee is charged on the declared limit, not on the ~200k default.
            self.assertEqual(limit, ("ComputeBudget111111111111111111111111111111", [], struct.pack("<BI", 2, 60_000)))
            self.assertEqual(price, ("ComputeBudget111111111111111111111111111111", [], struct.pack("<BQ", 3, 1200)))
            self.assertEqual(verify[0], "Ed25519SigVerify111111111111111111111111111")
            self.assertEqual(submit[0], gate)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        intent, raw = sent[0]
        # The receipt names the exact transaction, so a restart can settle it.
        self.assertEqual(intent["signature"], to_base58(raw[1:65]))
        self.assertEqual((intent["last_height"], intent["verifier"], intent["buffer"]), (77, KEYPAIR.address,
                                                                                        bundle["buffer"]))
        self.assertEqual((intent["config"], intent["evidence"]), (bundle["config"], evidence))
        self.assertEqual(bundle["confirmed_signature"], "sig")


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

    def test_remote_build_round_trip_through_the_restricted_command(self):
        """The verifier's request is exactly what the builder's forced SSH command reads."""
        import worker_build
        from worker_build import remote_build
        policy = SimpleNamespace(repo_url="https://github.com/example/program", build_subdir=None,
                                 library_name="program", build_features=("candidate",))
        commit, elf = "ab" * 20, b"\x7fELF" + bytes(60)
        builds, commands = [], []

        def build_candidate(repo_url, commit, subdir, library, features, work, toolchain):
            builds.append((repo_url, commit, subdir, library, features, toolchain))
            (work / "program.so").write_bytes(elf)
            return work / "program.so"

        def ssh(command, input, stdout, stderr, timeout):
            # Stand in for sshd: run the forced command with the request on stdin.
            commands.append((command, timeout))
            with patch.object(worker_build.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(input))), \
                    patch.object(worker_build.sys, "stdout", SimpleNamespace(buffer=stdout)):
                worker_build.serve_build()
            return SimpleNamespace(returncode=0)

        with TemporaryDirectory() as tmp, patch("worker_build.build_candidate", side_effect=build_candidate), \
                patch("worker_build.subprocess.run", side_effect=ssh):
            output = remote_build("canary-builder-a.internal", policy, commit, Path(tmp) / "build")
            self.assertEqual(output.read_bytes(), elf)
        self.assertEqual(builds, [("https://github.com/example/program", commit, ".", "program", ["candidate"], None)])
        (command, timeout), = commands
        self.assertEqual((command[:4], timeout), (["sh", "-c", 'ulimit -f 20480 && exec "$@"', "sh"], 5400))
        for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                       "UserKnownHostsFile=/run/canary/known_hosts"):
            self.assertIn(option, command)
        self.assertEqual(command[-5:], ["-i", "/run/canary/build_key", "-p", "2222", "builder@canary-builder-a.internal"])

    def test_remote_build_failures_are_build_failures(self):
        from worker_build import BuildFailed, remote_build
        policy = SimpleNamespace(repo_url="https://github.com/example/program", build_subdir=None,
                                 library_name="program", build_features=())

        def returns(code, data):
            def run(command, input, stdout, stderr, timeout):
                stdout.write(data)
                return SimpleNamespace(returncode=code)
            return run

        def times_out(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 5400)

        cases = {"ssh exit status": returns(255, b"\x7fELF"), "empty output": returns(0, b""),
                 "oversized output": returns(0, bytes(10 * 1024 * 1024 + 1)), "no ssh client": FileNotFoundError,
                 "timeout": times_out}
        with TemporaryDirectory() as tmp:
            for name, run in cases.items():
                with self.subTest(name), patch("worker_build.subprocess.run", side_effect=run), \
                        self.assertRaises(BuildFailed):
                    remote_build("canary-builder-a.internal", policy, "ab" * 20, Path(tmp) / name)
            with patch("worker_build.subprocess.run") as run:
                for host in ("builder host", "-oProxyCommand=sh", "Builder.internal", ""):
                    with self.subTest(host=host), self.assertRaisesRegex(BuildFailed, "hostname"):
                        remote_build(host, policy, "ab" * 20, Path(tmp) / "host")
                run.assert_not_called()

    def test_serve_build_refuses_oversized_requests(self):
        import worker_build
        with patch.object(worker_build.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b" " * 8193))), \
                patch("worker_build.build_candidate") as build, \
                self.assertRaisesRegex(worker_build.BuildFailed, "too large"):
            worker_build.serve_build()
        build.assert_not_called()

    def test_keygen_prints_secret_on_stdout_only(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("solana_tx.py")), "keygen"],
                                capture_output=True, text=True, check=True)
        keypair = Keypair(json.loads(result.stdout))
        self.assertEqual(result.stderr.strip(), keypair.address)
        self.assertNotIn("[", result.stderr)


if __name__ == "__main__":
    unittest.main()
