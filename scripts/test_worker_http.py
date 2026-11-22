import base64
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from uuid import uuid4

from devnet import to_base58
from fly_listener import Admissions, GATE, customer_policies
from live_capture import EMERGENCY, GATE as EVENT_GATE, UPGRADE, UnsupportedReplayVersion, capture, gate_events
from worker_http import start_http
from worker_notify import post_customer, public_address


class WorkerTest(unittest.TestCase):
    def test_supported_window_fails_closed_on_target_v1(self):
        target_bytes = bytes([4]) * 32
        target = to_base58(target_bytes)
        other = to_base58(bytes([5]) * 32)
        snapshot = dict(target=list(target_bytes), anchor_slot=100, replay_slots=2)
        calls = []

        def rpc(method, *args):
            calls.append((method, args))
            if method == "getSlot":
                return 104
            if method == "getSignaturesForAddress":
                return [dict(slot=102, signature=sig, err=None) for sig in ("unrelated", "target-v1")]
            if method == "getBlock":
                self.assertEqual(args[1]["maxSupportedTransactionVersion"], 1)
                self.assertEqual(args[1]["encoding"], "json")
                return {"transactions": [
                    dict(version=1, meta=dict(err=None, innerInstructions=[]),
                         transaction={"signatures": [sig],
                                      "message": {"accountKeys": [target, other],
                                                  "instructions": [{"programIdIndex": index}]}})
                    for sig, index in [("unrelated", 1), ("target-v1", 0)]
                ]}
            self.fail(f"unexpected RPC call {method}")

        with patch("live_capture.rpc", side_effect=rpc):
            with self.assertRaisesRegex(UnsupportedReplayVersion, "target-v1"):
                capture(snapshot, 500)
        self.assertEqual([method for method, _ in calls].count("getBlock"), 1)

    def test_same_slot_records_share_two_block_reads(self):
        target_bytes = bytes([4]) * 32
        target = to_base58(target_bytes)
        signature_bytes = [bytes([value]) * 64 for value in (1, 2)]
        signatures = list(map(to_base58, signature_bytes))
        entries = [dict(slot=102, signature=sig, err=None) for sig in reversed(signatures)]
        decoded = [
            {"meta": dict(err=None, innerInstructions=[]), "transaction": {
                "signatures": [sig], "message": {"accountKeys": [target], "instructions": [{"programIdIndex": 0}]},
            }} for sig in signatures
        ]
        encoded = [{"transaction": [base64.b64encode(b"\x01" + raw).decode(), "base64"]}
                   for raw in signature_bytes]
        snapshot = dict(target=list(target_bytes), anchor_slot=100, replay_slots=2)
        calls = []
        def rpc(method, *args):
            if method == "getSlot":
                return 104
            if method == "getSignaturesForAddress":
                return entries
            self.assertEqual(method, "getBlock")
            mode = args[1]["encoding"]
            calls.append(mode)
            return {"blockhash": "finalized", "transactions": decoded if mode == "json" else encoded}
        with patch("live_capture.rpc", side_effect=rpc):
            archive = capture(snapshot, 2)
        self.assertEqual(calls, ["json", "base64"])
        self.assertEqual([record["index"] for record in archive["records"]], [0, 1])
        self.assertEqual([bytes.fromhex(record["transaction"])[1:] for record in archive["records"]], signature_bytes)

    def test_customer_webhook_rejects_mixed_dns_and_never_follows_redirects(self):
        addresses = [(None, None, None, None, (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")]
        with patch("worker_notify.socket.getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(ValueError, "non-public"):
                public_address("customer.example", 443)
        with patch("worker_notify.HTTPSConnection") as connection, patch(
                "worker_notify.public_address", return_value="8.8.8.8"):
            conn = connection.return_value
            conn.port = 443
            conn.getresponse.return_value.status = 307
            with self.assertRaises(OSError):
                post_customer("https://customer.example/events", "{}")
            connection.assert_called_once_with("customer.example", 443, timeout=3)
            conn.request.assert_called_once()
            conn.close.assert_called_once()

    def test_finalized_gate_lifecycle_decoding(self):
        proposal = bytes([1]) * 32
        target = bytes([2]) * 32
        buffer = bytes([3]) * 32
        authority = bytes([4]) * 32
        lines = [f"Program {EVENT_GATE} invoke [1]"]
        for event in (UPGRADE + proposal + target, EMERGENCY + target + buffer + authority):
            lines.append("Program data: " + base64.b64encode(event).decode())
        lines.append(f"Program {EVENT_GATE} success")
        results = list(gate_events(lines))
        self.assertEqual([name for name, _ in results], ["UpgradeExecuted", "EmergencyOverrideTriggered"])
        self.assertEqual(results[0][1]["proposal"], to_base58(proposal))
        self.assertEqual(results[1][1]["authority"], to_base58(authority))
        self.assertEqual(list(gate_events(lines[1:-1])), [])

    def test_repo_configuration_and_authenticated_http(self):
        target = to_base58(bytes([5]) * 32)
        configured = {
            "targets": [target], "plan": "pro", "repo_url": "https://github.com/example/program",
            "library_name": "vault_program", "build_subdir": "programs/vault",
        }
        controls = customer_policies(json.dumps({"client": configured}))
        self.assertEqual(controls[target].build_subdir, "programs/vault")
        with self.assertRaisesRegex(ValueError, "inside"):
            customer_policies(json.dumps({"client": dict(configured, build_subdir="../outside")}))
        with self.assertRaisesRegex(ValueError, "credential-free"):
            customer_policies(json.dumps({"client": dict(configured, repo_url="https://secret@example.com/repo")}))
        db = Path.cwd() / f"http-test-{uuid4().hex}.sqlite3"
        wake = Event()
        try:
            now = datetime.now(timezone.utc)
            with closing(sqlite3.connect(db)) as connection:
                ledger = Admissions(connection, controls)
                ledger.consume({"target": target, "proposal": "example"}, now, enqueue=True)
                ledger.finish("example", now, "refused", "test reason")
            with patch.dict(environ, CANARY_ADMIN_TOKEN="test-admin", HELIUS_WEBHOOK_AUTHORIZATION="test-hook"):
                server = start_http(db, GATE, lambda: controls, wake, ("127.0.0.1", 0))
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(Request(base + "/webhook/helius", b"[{}]"), timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)
                unauthorized.exception.close()
                self.assertFalse(wake.is_set())
                request = Request(base + "/webhook/helius", b"[{}]", {"Authorization": "test-hook"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(wake.is_set())
                request = Request(base + "/status", headers={"Authorization": "Bearer test-admin"})
                with urlopen(request, timeout=2) as response:
                    report = json.load(response)
                self.assertEqual(report["active_customers"]["client"]["used"], 1)
                self.assertEqual(report["proposals"][0]["status"], "refused")
                self.assertNotIn("github.com", json.dumps(report))
            finally:
                server.shutdown()
                server.server_close()
        finally:
            db.unlink(missing_ok=True)
