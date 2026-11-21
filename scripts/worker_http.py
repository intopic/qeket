"""Authenticated wake-up and read-only status for the Fly worker."""
from contextlib import closing
from datetime import datetime, timezone
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from threading import Thread


def status(database, gate, targets):
    now = datetime.now(timezone.utc)
    customers = {
        policy.customer: {"monthly_limit": policy.monthly, "used": 0,
                          "max_replay_txs": policy.max_replay_txs}
        for policy in targets.values() if policy.active and now < policy.expires
    }
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        counts = connection.execute("""
            SELECT customer, count(*) FROM admissions
            WHERE gate = ? AND month = ? GROUP BY customer
        """, (gate, now.strftime("%Y-%m"))).fetchall()
        for identifier, count in counts:
            if identifier in customers:
                customers[identifier]["used"] = count
        jobs = connection.execute("""
            SELECT j.customer, j.proposal, j.started_at, j.finished_at, o.status, o.reason
            FROM customer_jobs j LEFT JOIN job_outcomes o
              ON o.gate = j.gate AND o.proposal = j.proposal
            WHERE j.gate = ? ORDER BY j.started_at DESC, j.proposal DESC LIMIT 100
        """, (gate,)).fetchall()
    return {
        "active_customers": customers,
        "proposals": [
            dict(customer=customer, proposal=proposal, started_at=started, finished_at=finished,
                 status=outcome or ("closed" if finished is not None else "queued"), reason=reason)
            for customer, proposal, started, finished, outcome, reason in jobs
        ],
    }


def start_http(database, gate, load_policies, wake, address=None, ready=None):
    if address is None:
        if "CANARY_HTTP_PORT" not in os.environ:
            return None
        address = ("0.0.0.0", int(os.environ["CANARY_HTTP_PORT"]))
    helius_auth = os.environ.get("HELIUS_WEBHOOK_AUTHORIZATION")
    admin_token = os.environ.get("CANARY_ADMIN_TOKEN")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_):
            pass

        def respond(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, expected):
            actual = self.headers.get("Authorization", "").encode()
            return bool(expected) and compare_digest(actual, expected.encode())

        def do_GET(self):
            if self.path == "/health":
                healthy = ready is None or ready()
                return self.respond(200 if healthy else 503, {"ready": healthy})
            if self.path != "/status":
                return self.respond(404, {"error": "not found"})
            if not self.authorized(f"Bearer {admin_token}" if admin_token else None):
                return self.respond(401, {"error": "unauthorized"})
            try:
                report = status(database, gate, load_policies())
            except (OSError, ValueError, KeyError, sqlite3.Error):
                return self.respond(503, {"error": "status unavailable"})
            return self.respond(200, report)

        def do_POST(self):
            if self.path != "/webhook/helius":
                return self.respond(404, {"error": "not found"})
            if not self.authorized(helius_auth):
                return self.respond(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.respond(400, {"error": "invalid length"})
            if not 0 < length <= 1024 * 1024:
                return self.respond(413, {"error": "invalid webhook size"})
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, OSError):
                return self.respond(400, {"error": "invalid JSON"})
            if not isinstance(body, (list, dict)) or not body:
                return self.respond(400, {"error": "expected webhook object or batch"})
            # A webhook can request a poll. Only finalized RPC data can enqueue a proposal.
            wake.set()
            return self.respond(202, {"accepted": True})

    server = ThreadingHTTPServer(address, Handler)
    Thread(target=server.serve_forever, daemon=True, name="canary-http").start()
    return server
