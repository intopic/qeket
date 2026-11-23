import json
import sqlite3
from uuid import uuid4
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
from unittest.mock import Mock, patch

from devnet import to_base58
from fly_listener import (GATE, LIVE_CACHE_BYTES, Admissions, check_live_traffic, collect_captures, customer_policies,
                          observe_withdrawal, reconcile_votes, withdrawn_jobs)
from live_prestate import PrestateRefused
from worker_build import BuildFailed
from worker_vote import CandidateRejected, ReplayIncomplete, VoteRefused

VOTED = dict(status="voted", signature="sig", verifier="node", diff_root="aa", replayed_tx_count=2, max_cu_delta_bps=0)


def settled(result=None, error=None):
    future = Future()
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)
    return future


class QueuedExecutor:
    """Accepts work and never starts it, like a pool whose worker is busy."""

    def __init__(self):
        self.futures = []

    def submit(self, function, *args):
        self.futures.append(Future())
        return self.futures[-1]


def ledger_for(db, now, count):
    """Admissions with one active live pre-state job per customer."""
    targets = [to_base58(bytes([10 + n]) * 32) for n in range(count)]
    ledger = Admissions(db, customer_policies(json.dumps(
        {f"c{n}": {"targets": [target], "seed_accounts": []} for n, target in enumerate(targets)})))
    proposals = [to_base58(bytes([40 + n]) * 32) for n in range(count)]
    for n, (target, proposal) in enumerate(zip(targets, proposals)):
        ledger.consume(dict(target=target, proposal=proposal, nonce=n, anchor_slot=100, replay_slots=10),
                       now, enqueue=True)
    return ledger, proposals


def outcome_of(db, proposal):
    return db.execute("SELECT status, reason FROM job_outcomes WHERE proposal = ?", (proposal,)).fetchone()


class AttestationPipelineTest(unittest.TestCase):
    """After the replay preflight, each node builds, verifies and votes on its own."""

    def setUp(self):
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.target, self.proposal = [to_base58(bytes([n]) * 32) for n in (1, 2)]
        self.event = dict(target=self.target, proposal=self.proposal, anchor_slot=100, replay_slots=10)
        self.policies = customer_policies(json.dumps({"client": {
            "targets": [self.target], "seed_accounts": [], "repo_url": "https://github.com/example/program",
            "library_name": "program", "build_subdir": "programs/program", "build_features": ["candidate"],
        }}))
        self.notice = Mock()

    def run_cycles(self, attestor, slots=(111,), archive=None, policies=None):
        cache = {self.proposal: {"expires_at": self.now.timestamp() + 60}}
        with closing(sqlite3.connect(":memory:")) as db:
            ledger = Admissions(db, policies or self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            with patch("fly_listener.replay_archive", return_value=archive or {"records": [1, 2]}) as replay:
                for slot in slots:
                    check_live_traffic(ledger, cache, slot, self.now, self.notice, attestor)
            outcome = db.execute("SELECT status, reason FROM job_outcomes").fetchone()
            return ledger.active_jobs(), cache, outcome, replay

    def test_vote_closes_the_job(self):
        attestor = Mock(return_value=dict(status="voted", signature="sig", verifier="node", diff_root="aa",
                                          replayed_tx_count=2, max_cu_delta_bps=0))
        active, cache, outcome, replay = self.run_cycles(attestor, slots=(110, 111, 112))
        replay.assert_called_once()
        attestor.assert_called_once()
        event, bundle, policy = attestor.call_args.args
        self.assertEqual(bundle["archive"], {"records": [1, 2]})
        self.assertEqual(policy.build_features, ("candidate",))
        self.assertEqual((active, cache), ([], {}))
        self.assertEqual(outcome, ("voted", "vote sig; diff_root aa"))
        self.assertEqual(self.notice.call_args.args[0], "VoteSubmitted")
        self.assertEqual(self.notice.call_args.args[1]["signature"], "sig")

    def test_rejected_candidate_and_build_failures_are_final(self):
        active, _, outcome, _ = self.run_cycles(Mock(side_effect=CandidateRejected("post-state differs")))
        self.assertEqual((active, outcome), ([], ("rejected", "post-state differs")))
        self.assertEqual(self.notice.call_args.args[0], "CandidateRejected")
        failing = Mock(side_effect=BuildFailed("cargo exited with 101"))
        active, cache, outcome, _ = self.run_cycles(failing, slots=(111, 112))
        self.assertEqual((len(active), outcome[0]), (1, "traffic_preflight_passed"))
        self.assertEqual(cache[self.proposal]["build_attempts"], 2)
        active, _, outcome, _ = self.run_cycles(failing, slots=(111, 112, 113))
        self.assertEqual((active, outcome), ([], ("refused", "candidate build failed: cargo exited with 101")))

    def test_incomplete_replay_is_not_a_candidate_rejection(self):
        active, _, outcome, _ = self.run_cycles(Mock(side_effect=ReplayIncomplete("baseline failed at 1:0")))
        self.assertEqual((active, outcome), ([], ("refused", "replay evidence incomplete: baseline failed at 1:0")))
        self.assertEqual(self.notice.call_args.args[0], "ReplayFailed")

    def test_verification_runs_beside_the_event_loop(self):
        release = Event()
        attestor = Mock(side_effect=lambda *_: release.wait(10) and dict(
            status="voted", signature="sig", verifier="node", diff_root="aa", replayed_tx_count=2,
            max_cu_delta_bps=0))
        cache = {self.proposal: {"expires_at": self.now.timestamp() + 60}}
        running = {}
        with closing(sqlite3.connect(":memory:")) as db, ThreadPoolExecutor(max_workers=1) as pool, patch(
                "fly_listener.replay_archive", return_value={"records": [1]}):
            ledger = Admissions(db, self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            started = time.monotonic()
            check_live_traffic(ledger, cache, 111, self.now, self.notice, attestor, pool, running)
            # The listener returns to event ingestion while the build is still running.
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual((len(ledger.active_jobs()), list(running)), (1, [self.proposal]))
            release.set()
            running[self.proposal].result(timeout=10)
            check_live_traffic(ledger, cache, 112, self.now, self.notice, attestor, pool, running)
            self.assertEqual(ledger.active_jobs(), [])
            self.assertEqual(db.execute("SELECT status FROM job_outcomes").fetchone(), ("voted",))
            self.assertEqual(attestor.call_count, 1)
        self.assertEqual(running, {})

    def test_upgrade_during_the_vote_keeps_both_outcomes(self):
        from fly_listener import observe_upgrade
        release = Event()
        attestor = Mock(side_effect=lambda *_: release.wait(10) and dict(
            status="voted", signature="sig", verifier="node", diff_root="aa", replayed_tx_count=2,
            max_cu_delta_bps=0))
        cache = {self.proposal: {"expires_at": self.now.timestamp() + 60}}
        running = {}
        with closing(sqlite3.connect(":memory:")) as db, ThreadPoolExecutor(max_workers=1) as pool, patch(
                "fly_listener.replay_archive", return_value={"records": [1]}):
            ledger = Admissions(db, self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            check_live_traffic(ledger, cache, 111, self.now, self.notice, attestor, pool, running)
            # Other verifiers reached quorum. The gate executed while this vote finalizes.
            observe_upgrade(ledger, cache, running, None, self.proposal, self.now)
            self.assertEqual(len(ledger.active_jobs()), 1)
            release.set()
            running[self.proposal].result(timeout=10)
            check_live_traffic(ledger, cache, 112, self.now, self.notice, attestor, pool, running)
            self.assertEqual(db.execute("SELECT status, reason FROM job_outcomes").fetchone(),
                             ("executed", "vote sig; diff_root aa; upgrade executed"))
            # Without a job in flight the upgrade closes the job directly.
            other = dict(self.event, proposal=to_base58(bytes([9]) * 32))
            ledger.policies = customer_policies(json.dumps({"other": {"targets": [self.target]}}))
            ledger.consume(other, self.now.replace(month=12, day=2), enqueue=True)
            observe_upgrade(ledger, {other["proposal"]: {}}, {}, None, other["proposal"], self.now)
            self.assertEqual(db.execute("SELECT status, reason FROM job_outcomes WHERE proposal = ?",
                                        (other["proposal"],)).fetchone(),
                             ("executed", "upgrade executed before this node finished verification"))

    def test_outcome_discards_the_job_files(self):
        from fly_listener import Attestor
        with TemporaryDirectory() as tmp:
            job = Path(tmp) / self.proposal
            job.mkdir()
            (job / "archive.json").write_text("{}")
            (job / "evidence.json").write_text("{}")
            attestor = Attestor("canary-gate", "toolchain", Path(tmp), None, "gate")
            with patch.object(Attestor, "__call__", side_effect=CandidateRejected("post-state differs")):
                self.run_cycles(attestor)
            self.assertEqual(sorted(p.name for p in job.iterdir()), ["evidence.json"])
            (job / "archive.json").write_text("{}")
            keep = Attestor("canary-gate", "toolchain", Path(tmp), None, "gate", keep_files=True)
            keep.discard(self.proposal)
            keep.discard_all()
            self.assertTrue((job / "archive.json").exists())
            attestor.discard_all()
            self.assertFalse((job / "archive.json").exists())

    def test_transient_errors_keep_the_job(self):
        attestor = Mock(side_effect=RuntimeError("getSlot: endpoint unavailable"))
        active, cache, outcome, _ = self.run_cycles(attestor, slots=(111, 112))
        self.assertEqual((len(active), outcome[0], attestor.call_count), (1, "traffic_preflight_passed", 2))
        self.assertIn("archive", cache[self.proposal])
        self.notice.assert_not_called()

    def test_build_source_is_required(self):
        policies = customer_policies(json.dumps({"client": {"targets": [self.target], "seed_accounts": []}}))
        attestor = Mock()
        active, _, outcome, _ = self.run_cycles(attestor, policies=policies)
        attestor.assert_not_called()
        self.assertEqual((active, outcome), ([], ("refused", "customer build source is not configured")))

    def test_vote_outcome_survives_execution(self):
        with closing(sqlite3.connect(":memory:")) as db:
            ledger = Admissions(db, self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            ledger.finish(self.proposal, self.now, "voted", "vote sig")
            self.assertFalse(ledger.finish(self.proposal, self.now, "executed"))
            ledger.record_executed(self.proposal)
            self.assertEqual(db.execute("SELECT status, reason FROM job_outcomes").fetchone(),
                             ("executed", "vote sig; upgrade executed"))

    def test_build_features_are_cargo_names(self):
        for features in (["a b"], ["--all-features"], "candidate", [f"f{i}" for i in range(17)]):
            with self.subTest(features=features), self.assertRaisesRegex(ValueError, "build_features"):
                customer_policies(json.dumps({"c": {"targets": [self.target], "build_features": features}}))

    def run_until_expired(self, attestor, finish_before_expiry=False):
        """Expire the proposal while its job runs in the worker thread."""
        started, release = Event(), Event()

        def blocked(event, bundle, policy):
            started.set()
            release.wait(10)
            return attestor(event, bundle, policy)

        work = Mock(side_effect=blocked)
        cache = {self.proposal: {"expires_at": self.now.timestamp() + 60}}
        later = self.now + timedelta(minutes=2)
        running = {}
        with closing(sqlite3.connect(":memory:")) as db, ThreadPoolExecutor(max_workers=1) as pool, patch(
                "fly_listener.replay_archive", return_value={"records": [1]}):
            ledger = Admissions(db, self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            check_live_traffic(ledger, cache, 111, self.now, self.notice, work, pool, running)
            self.assertTrue(started.wait(10))
            if finish_before_expiry:
                release.set()
                running[self.proposal].result(timeout=10)
            check_live_traffic(ledger, cache, 112, later, self.notice, work, pool, running)
            if not finish_before_expiry:
                # The job stays open until the worker thread stops, and may not vote.
                self.assertEqual(len(ledger.active_jobs()), 1)
                self.assertTrue(cache[self.proposal]["cancelled"])
                release.set()
                running[self.proposal].result(timeout=10)
                check_live_traffic(ledger, cache, 113, later, self.notice, work, pool, running)
            self.assertEqual((ledger.active_jobs(), cache, running), ([], {}, {}))
            return db.execute("SELECT status, reason FROM job_outcomes").fetchone()

    def test_expiry_during_a_running_job_blocks_its_vote(self):
        seen = []

        def attestor(event, bundle, policy):
            seen.append(bundle.get("cancelled"))
            if bundle.get("cancelled"):  # Attestor's authorize() refuses before signing.
                raise VoteRefused("proposal or customer authorization expired or changed")
            return VOTED

        self.assertEqual(self.run_until_expired(attestor), ("refused", "proposal expired"))
        self.assertEqual(seen, [True])

    def test_vote_signed_before_expiry_is_kept(self):
        outcome = self.run_until_expired(Mock(return_value=VOTED), finish_before_expiry=True)
        self.assertEqual(outcome, ("voted", "vote sig; diff_root aa"))
        self.assertEqual(self.notice.call_args.args[0], "VoteSubmitted")

    def test_queued_job_is_cancelled_on_expiry(self):
        attestor, queue = Mock(), QueuedExecutor()
        cache = {self.proposal: {"expires_at": self.now.timestamp() + 60}}
        running = {}
        with closing(sqlite3.connect(":memory:")) as db:
            ledger = Admissions(db, self.policies)
            ledger.consume(self.event, self.now, enqueue=True)
            check_live_traffic(ledger, cache, 111, self.now, self.notice, attestor, queue, running)
            check_live_traffic(ledger, cache, 112, self.now + timedelta(minutes=2), self.notice, attestor, queue,
                               running)
            self.assertTrue(queue.futures[0].cancelled())
            self.assertEqual((ledger.active_jobs(), cache, running), ([], {}, {}))
            self.assertEqual(outcome_of(db, self.proposal), ("refused", "proposal expired"))
        attestor.assert_not_called()

    def test_vote_is_authorized_against_the_current_customer_policy(self):
        from fly_listener import Attestor
        calls = []
        current = {"policies": self.policies}
        with TemporaryDirectory() as tmp, patch("fly_listener.attest", side_effect=lambda *args: calls.append(args)):
            attestor = Attestor("canary-gate", None, Path(tmp), None, "gate",
                                load_policies=lambda: current["policies"])
            bundle = {"expires_at": time.time() + 60}
            attestor(self.event, bundle, self.policies[self.target])
            authorize = calls[0][-1]
            authorize()
            # The customer changed the build source while this job ran.
            current["policies"] = customer_policies(json.dumps({"client": {
                "targets": [self.target], "seed_accounts": [], "repo_url": "https://github.com/example/fork",
                "library_name": "program", "build_subdir": "programs/program", "build_features": ["candidate"],
            }}))
            with self.assertRaisesRegex(VoteRefused, "changed"):
                authorize()
            current["policies"] = {}
            with self.assertRaisesRegex(VoteRefused, "changed"):
                authorize()
            current["policies"] = self.policies
            bundle["cancelled"] = True
            with self.assertRaisesRegex(VoteRefused, "expired"):
                authorize()
            del bundle["cancelled"]
            bundle["expires_at"] = time.time() - 1
            with self.assertRaisesRegex(VoteRefused, "expired"):
                authorize()


class LiveCaptureTest(unittest.TestCase):
    """Live pre-state reads run beside the event loop and are collected at checkpoints."""

    def setUp(self):
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.notice = Mock()

    def test_finished_captures_enter_the_cache_or_refuse_their_job(self):
        alpenglow = "replay window is too close to the Alpenglow activation"
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (ready, pending, refused, broken) = ledger_for(db, self.now, 4)
            waiting = Future()
            captures = {ready: settled(dict(encoded_bytes=10, capture_slot=105)), pending: waiting,
                        refused: settled(error=PrestateRefused(alpenglow)),
                        broken: settled(error=RuntimeError("getMultipleAccounts: endpoint unavailable"))}
            live = {}
            collect_captures(ledger, captures, live, self.now, self.notice)
            self.assertEqual((list(live), captures), ([ready], {pending: waiting}))
            self.assertEqual(outcome_of(db, ready), ("prestate_captured", "live state captured at slot 105"))
            self.assertIsNone(outcome_of(db, pending))
            self.assertEqual(outcome_of(db, refused), ("refused", alpenglow))
            self.assertEqual(outcome_of(db, broken), ("refused", "unable to read consistent live pre-state"))
            self.assertEqual({job["proposal"] for job in ledger.active_jobs()}, {ready, pending})
            self.assertEqual([c.args for c in self.notice.call_args_list], [
                ("ReplayFailed", dict(proposal=refused, reason=alpenglow)),
                ("ReplayFailed", dict(proposal=broken, reason="unable to read consistent live pre-state")),
            ])

    def test_full_cache_refuses_and_closed_jobs_are_not_cached(self):
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (full, closed) = ledger_for(db, self.now, 2)
            live = {"cached": dict(encoded_bytes=LIVE_CACHE_BYTES)}
            collect_captures(ledger, {full: settled(dict(encoded_bytes=1, capture_slot=105))}, live, self.now,
                             self.notice)
            self.assertEqual((list(live), outcome_of(db, full)), (["cached"], ("refused", "live pre-state cache is full")))
            # The operator closed this job while its capture was still running.
            ledger.finish(closed, self.now, "closed")
            live = {}
            collect_captures(ledger, {closed: settled(dict(encoded_bytes=1, capture_slot=105))}, live, self.now,
                             self.notice)
            self.assertEqual((live, outcome_of(db, closed)), ({}, ("closed", None)))
            self.assertEqual(self.notice.call_count, 1)

    def test_failed_capture_logs_its_cause_for_the_operator(self):
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (broken,) = ledger_for(db, self.now, 1)
            captures = {broken: settled(error=RuntimeError("getProgramAccounts: HTTP 429"))}
            with patch("builtins.print") as log:
                collect_captures(ledger, captures, {}, self.now, self.notice)
            # The customer sees the generic reason, the operator log the cause.
            self.assertEqual(outcome_of(db, broken), ("refused", "unable to read consistent live pre-state"))
            logged = json.loads(log.call_args.args[0])
            self.assertEqual(logged, dict(event="prestate_capture_failed", proposal=broken,
                                          error="RuntimeError: getProgramAccounts: HTTP 429"))


class WithdrawalTest(unittest.TestCase):
    """A cancelled or invalidated proposal closes its job, so the customer's next proposal is admitted."""

    def setUp(self):
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.notice = Mock()

    def test_cancel_before_the_capture_finishes_frees_the_customer(self):
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (proposal,) = ledger_for(db, self.now, 1)
            capture, live = Future(), {}
            captures = {proposal: capture}
            jobs = withdrawn_jobs(ledger, "ProposalCancelled", {"proposal": proposal})
            self.assertEqual(jobs, [(proposal, "proposal cancelled by governance")])
            for job, reason in jobs:
                observe_withdrawal(ledger, live, captures, None, job, self.now, reason, self.notice)
            self.assertTrue(capture.cancelled())
            self.assertEqual((captures, ledger.active_jobs()), ({}, []))
            self.assertEqual(outcome_of(db, proposal), ("cancelled", "proposal cancelled by governance"))
            self.notice.assert_called_once_with(
                "ProposalWithdrawn", {"proposal": proposal, "reason": "proposal cancelled by governance"})
            # Unrelated cancellations change nothing.
            self.assertEqual(withdrawn_jobs(ledger, "ProposalCancelled", {"proposal": to_base58(bytes(32))}), [])
            target, = ledger.policies
            later = dict(target=target, proposal=to_base58(bytes([99]) * 32), nonce=1, anchor_slot=200,
                         replay_slots=10)
            self.assertEqual(ledger.consume(later, self.now, enqueue=True), "c0")

    def test_cancel_during_a_queued_replay_closes_it_without_a_vote(self):
        attestor, queue, running = Mock(), QueuedExecutor(), {}
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (proposal,) = ledger_for(db, self.now, 1)
            live = {proposal: {"expires_at": self.now.timestamp() + 600}}
            check_live_traffic(ledger, live, 111, self.now, self.notice, attestor, queue, running)
            self.assertIn(proposal, running)
            observe_withdrawal(ledger, live, {}, attestor, proposal, self.now, "proposal cancelled by governance",
                               self.notice)
            # The job closes on the next checkpoint, after its worker stops.
            self.assertEqual(len(ledger.active_jobs()), 1)
            check_live_traffic(ledger, live, 112, self.now, self.notice, attestor, queue, running)
            self.assertTrue(queue.futures[0].cancelled())
            self.assertEqual((ledger.active_jobs(), live, running), ([], {}, {}))
            self.assertEqual(outcome_of(db, proposal), ("cancelled", "proposal cancelled by governance"))
        attestor.assert_not_called()
        self.notice.assert_called_once_with(
            "ProposalWithdrawn", {"proposal": proposal, "reason": "proposal cancelled by governance"})

    def test_policy_update_withdraws_the_old_generation_of_that_gate_config(self):
        from solana_tx import find_program_address
        from devnet import from_base58
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, (old, other) = ledger_for(db, self.now, 2)
            targets = {job["proposal"]: job["target"] for job in ledger.active_jobs()}
            config = find_program_address([b"gatekeeper_cfg", from_base58(targets[old])], GATE)[0]
            reason = "gate policy changed; the proposal can no longer execute"
            self.assertEqual(withdrawn_jobs(ledger, "PolicyUpdated", {"config": config, "next_nonce": 1 << 32}),
                             [(old, reason)])
            # A proposal of the new generation stays.
            self.assertEqual(withdrawn_jobs(ledger, "PolicyUpdated", {"config": config, "next_nonce": 0}), [])


class InterruptedVoteTest(unittest.TestCase):
    """After a restart, a vote sent before it is settled from finalized chain state."""

    def test_interrupted_votes_settle_from_finalized_state(self):
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        notice = Mock()
        root = "ab" * 32
        with closing(sqlite3.connect(":memory:")) as db:
            ledger, proposals = ledger_for(db, now, 7)
            landed, executed, waiting, expired, late, offline, differs = proposals
            jobs = {job["proposal"]: job for job in ledger.active_jobs()}

            def receipt(last_height=500, **extra):
                return dict(evidence=dict(diff_root=[0xAB] * 32), verifier="node", last_height=last_height, **extra)

            interrupted = {p: (jobs[p], receipt()) for p in (landed, expired, late, offline, differs)}
            interrupted[executed] = (jobs[executed], receipt(executed=True))
            # Its blockhash is still valid at the finalized height 600.
            interrupted[waiting] = (jobs[waiting], receipt(last_height=900))
            answers = {landed: [True], executed: [True], waiting: [False], expired: [False, False],
                       late: [False, True], offline: [RuntimeError("getAccountInfo: endpoint unavailable")],
                       differs: [VoteRefused("finalized verifier vote has a different root")]}

            def finalized_vote(job, bundle, evidence, verifier, gate):
                self.assertEqual((verifier, gate), ("node", GATE))
                answer = answers[job["proposal"]].pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            with patch("fly_listener.finalized_vote", side_effect=finalized_vote), patch(
                    "fly_listener.rpc", return_value=600) as rpc:
                reconcile_votes(ledger, interrupted, now, notice)
                self.assertEqual(rpc.call_args.args, ("getBlockHeight", {"commitment": "finalized"}))
                self.assertEqual(outcome_of(db, landed), ("voted", f"vote confirmed on chain; diff_root {root}"))
                self.assertEqual(outcome_of(db, executed),
                                 ("executed", f"vote confirmed on chain; diff_root {root}; upgrade executed"))
                self.assertEqual(outcome_of(db, expired), ("refused", "interrupted vote expired without confirmation"))
                self.assertEqual(outcome_of(db, differs),
                                 ("refused", "vote reconciliation failed: finalized verifier vote has a different root"))
                # Still able to land, found on the re-read, or unreadable: the job stays open.
                self.assertEqual(set(interrupted), {waiting, late, offline})
                self.assertEqual({job["proposal"] for job in ledger.active_jobs()}, {waiting, late, offline})
                self.assertEqual([c.args[0] for c in notice.call_args_list], ["VoteSubmitted", "VoteSubmitted"])
                answers.update({waiting: [False], late: [True], offline: [True]})
                reconcile_votes(ledger, interrupted, now, notice)
            self.assertEqual(set(interrupted), {waiting})
            self.assertEqual(outcome_of(db, late)[0], "voted")
            self.assertEqual(outcome_of(db, offline)[0], "voted")


class AdmissionsTest(unittest.TestCase):
    def test_live_traffic_waits_for_finalization_and_closes_on_missing_state(self):
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        target, proposal = [to_base58(bytes([n]) * 32) for n in (1, 2)]
        event = dict(target=target, proposal=proposal, anchor_slot=100, replay_slots=10)
        policies = customer_policies(json.dumps({"client": {"targets": [target], "seed_accounts": []}}))
        cache = {proposal: {"expires_at": now.timestamp() + 60}}
        notice = Mock()
        with closing(sqlite3.connect(":memory:")) as db:
            ledger = Admissions(db, policies)
            ledger.consume(event, now, enqueue=True)
            with patch("fly_listener.replay_archive", side_effect=PrestateRefused("missing state")) as check:
                check_live_traffic(ledger, cache, 110, now, notice)
                check.assert_not_called()
                check_live_traffic(ledger, cache, 111, now, notice)
                check.assert_called_once()
            self.assertEqual(ledger.active_jobs(), [])
            self.assertEqual(cache, {})
            self.assertEqual(db.execute("SELECT status, reason FROM job_outcomes").fetchone(),
                             ("refused", "missing state"))
        notice.assert_called_once_with("ReplayFailed", {"proposal": proposal, "reason": "missing state"})

    def test_quota_and_cursor_survive_restart(self):
        first, second = [to_base58(bytes([n]) * 32) for n in (1, 2)]
        policies = customer_policies(json.dumps({"client": {
            "targets": [first, second],
            "active_until": "2027-01-01T00:00:00Z",
            "monthly_proposals": 1,
        }}))
        november = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)
        december = datetime(2026, 12, 1, tzinfo=timezone.utc)
        expiry = datetime(2027, 1, 1, tzinfo=timezone.utc)
        initial = {"target": first, "proposal": "initial"}
        next_proposal = {"target": second, "proposal": "next"}
        db = Path.cwd() / f"admissions-test-{uuid4().hex}.sqlite3"
        try:
            with closing(sqlite3.connect(db)) as connection:
                ledger = Admissions(connection, policies)
                self.assertIsNone(ledger.consume({"target": "unlisted"}, november))
                self.assertEqual(ledger.consume(initial, november), "client")
                self.assertIsNone(ledger.consume(next_proposal, november))
                ledger.checkpoint(100)
            with closing(sqlite3.connect(db)) as connection:
                ledger = Admissions(connection, policies)
                self.assertIsNone(ledger.consume(next_proposal, november))
                ledger.checkpoint(99)
                self.assertEqual(ledger.cursor(), 100)
                self.assertIsNone(ledger.consume(initial, december))
                self.assertEqual(ledger.consume(next_proposal, december), "client")
                self.assertIsNone(ledger.consume({"target": first}, expiry))
        finally:
            db.unlink(missing_ok=True)


    def test_active_job_and_cooldown(self):
        target = to_base58(bytes([1]) * 32)
        policies = customer_policies(json.dumps({"client": {
            "targets": [target], "plan": "pro", "cooldown_minutes": 5,
        }}))
        start = datetime(2026, 12, 1, tzinfo=timezone.utc)
        with closing(sqlite3.connect(":memory:")) as connection:
            ledger = Admissions(connection, policies)
            first = {"target": target, "proposal": "a"}
            second = {"target": target, "proposal": "b"}
            self.assertEqual(ledger.consume(first, start, enqueue=True), "client")
            self.assertIsNone(ledger.consume(second, start + timedelta(hours=1), enqueue=True))
            self.assertTrue(ledger.finish("a", start + timedelta(hours=1)))
            self.assertIsNone(ledger.consume(second, start + timedelta(hours=1, minutes=4), enqueue=True))
            self.assertEqual(ledger.consume(second, start + timedelta(hours=1, minutes=5), enqueue=True), "client")
            payload, = connection.execute("SELECT payload FROM customer_jobs WHERE proposal = 'b'").fetchone()
            self.assertEqual(json.loads(payload)["max_replay_txs"], 2000)
            ledger.policies = customer_policies(json.dumps({"client": {
                "targets": [target], "active": False,
            }}))
            self.assertIsNone(ledger.consume({"target": target, "proposal": "c"}, start, enqueue=True))

    def test_concurrent_admission_isolated_per_customer(self):
        targets = [to_base58(bytes([n]) * 32) for n in (1, 2, 3)]
        controls = customer_policies(json.dumps({
            f"client-{i}": {"targets": [target], "plan": plan}
            for i, (target, plan) in enumerate(zip(targets, ("starter", "pro", "enterprise")))
        }))
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        attempts = [(target, f"proposal-{i}-{n}") for i, target in enumerate(targets) for n in range(8)]
        barrier = Barrier(len(attempts))
        db = Path.cwd() / f"concurrent-admissions-test-{uuid4().hex}.sqlite3"
        try:
            with closing(sqlite3.connect(db)) as connection:
                Admissions(connection, controls)

            def submit(attempt):
                target, proposal = attempt
                with closing(sqlite3.connect(db, timeout=20)) as connection:
                    ledger = Admissions(connection, controls)
                    barrier.wait(timeout=20)
                    return ledger.consume({"target": target, "proposal": proposal}, now, enqueue=True)

            with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
                admitted = [client for client in pool.map(submit, attempts) if client is not None]
            self.assertCountEqual(admitted, ["client-0", "client-1", "client-2"])
            with closing(sqlite3.connect(db)) as connection:
                ledger = Admissions(connection, controls)
                jobs = {job["target"]: job["max_replay_txs"] for job in ledger.active_jobs()}
                self.assertEqual(jobs, dict(zip(targets, (500, 2000, 5000))))
                total, = connection.execute("SELECT count(*) FROM admissions").fetchone()
                self.assertEqual(total, 3)
        finally:
            db.unlink(missing_ok=True)


class ConfigurationTest(unittest.TestCase):
    def test_every_customer_gets_the_live_capture(self):
        target = to_base58(bytes([1]) * 32)
        mint = to_base58(bytes([2]) * 32)
        policies = customer_policies(json.dumps({
            "plain": {"targets": [target]},
            "with_mint": {"targets": [to_base58(bytes([3]) * 32)], "seed_accounts": [mint]},
        }))
        self.assertEqual(policies[target].seed_accounts, ())
        self.assertEqual(policies[to_base58(bytes([3]) * 32)].seed_accounts, (mint,))
        for seeds in (None, [mint, mint], [to_base58(bytes([n]) * 32) for n in range(97)], ["short"]):
            with self.subTest(seeds=seeds), self.assertRaisesRegex(ValueError, "seed_accounts"):
                customer_policies(json.dumps({"c": {"targets": [target], "seed_accounts": seeds}}))

    def test_cluster_must_match_the_rpc_genesis(self):
        from devnet import CLUSTER_GENESIS, check_cluster
        devnet, mainnet = CLUSTER_GENESIS["devnet"], CLUSTER_GENESIS["mainnet-beta"]
        self.assertEqual(check_cluster(devnet, {}), "devnet")
        self.assertEqual(check_cluster(mainnet, {"CANARY_CLUSTER": "mainnet-beta"}), "mainnet-beta")
        # Mainnet is never the default, and a Devnet worker refuses a Mainnet endpoint.
        with self.assertRaisesRegex(ValueError, "expected Solana devnet"):
            check_cluster(mainnet, {})
        with self.assertRaisesRegex(ValueError, "expected Solana mainnet-beta"):
            check_cluster(devnet, {"CANARY_CLUSTER": "mainnet-beta"})
        with self.assertRaisesRegex(ValueError, "CANARY_CLUSTER must be one of"):
            check_cluster(devnet, {"CANARY_CLUSTER": "testnet"})


if __name__ == "__main__":
    unittest.main()
