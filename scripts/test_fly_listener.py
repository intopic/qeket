import json
import sqlite3
from uuid import uuid4
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from devnet import to_base58
from fly_listener import Admissions, capture_ready, check_live_traffic, customer_policies
from live_prestate import PrestateRefused


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
            with patch("fly_listener.preflight_traffic", side_effect=PrestateRefused("missing state")) as check:
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

    def test_collect_only_after_window_and_only_once(self):
        target_bytes = bytes([7]) * 32
        target = to_base58(target_bytes)
        proposal = to_base58(bytes([8]) * 32)
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        event = dict(target=target, proposal=proposal, nonce=9, anchor_slot=100, replay_slots=10)
        root = Path.cwd() / f"capture-test-{uuid4().hex}"
        directory = root / proposal
        directory.mkdir(parents=True)
        snapshot = directory / "snapshot.json"
        archive = directory / "archive.json"
        try:
            snapshot.write_text(json.dumps(dict(target=list(target_bytes), nonce=9, anchor_slot=100, replay_slots=10)))
            with closing(sqlite3.connect(":memory:")) as connection:
                ledger = Admissions(connection, customer_policies(json.dumps({
                    "c": {"targets": [target], "plan": "starter"},
                })))
                ledger.consume(event, now, enqueue=True)
                with patch("fly_listener.capture", return_value={"records": [1]}) as collect:
                    capture_ready(ledger, root, 110, now)
                    collect.assert_not_called()
                    capture_ready(ledger, root, 111, now)
                    capture_ready(ledger, root, 112, now)
                    collect.assert_called_once_with(json.loads(snapshot.read_text()), 500)
                self.assertEqual(json.loads(archive.read_text()), {"records": [1]})
        finally:
            archive.unlink(missing_ok=True)
            snapshot.unlink(missing_ok=True)
            directory.rmdir()
            root.rmdir()


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
