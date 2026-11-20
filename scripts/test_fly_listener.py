import json
import sqlite3
from uuid import uuid4
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from devnet import to_base58
from fly_listener import Admissions, capture_ready, customer_policies


class AdmissionsTest(unittest.TestCase):
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
