"""Admission policy and durable monthly usage for the Fly Devnet listener."""
import argparse
import json
import os
import sqlite3
import time
from threading import Event
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from devnet import GENESIS, from_base58, to_base58
from live_capture import GATE, UnsupportedReplayVersion, capture, rpc, watch
from live_prestate import PrestateRefused, capture_prestate
from worker_http import start_http
from worker_notify import notify


PLANS = {"starter": (5, 500), "pro": (20, 2000), "enterprise": (None, 5000)}


@dataclass(frozen=True)
class Policy:
    customer: str
    expires: datetime
    monthly: int | None
    max_replay_txs: int
    cooldown_minutes: int
    active: bool
    repo_url: str | None
    library_name: str | None
    build_subdir: str | None
    webhook_url: str | None
    seed_accounts: tuple[str, ...] | None


def optional_https(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError(f"invalid {field}")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError(f"{field} must be a credential-free HTTPS URL")
    return value


def build_options(policy):
    repo = optional_https(policy.get("repo_url"), "repo_url")
    if repo and urlsplit(repo).query:
        raise ValueError("repo_url cannot contain a query string")
    webhook = optional_https(policy.get("webhook_url"), "webhook_url")
    library = policy.get("library_name")
    if library is not None and (not isinstance(library, str)
                                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", library)):
        raise ValueError("invalid library_name")
    subdir = policy.get("build_subdir")
    if subdir is not None and (
        not isinstance(subdir, str) or not subdir or chr(92) in subdir or ":" in subdir
        or any(ord(c) < 32 for c in subdir)
        or PurePosixPath(subdir).is_absolute() or ".." in PurePosixPath(subdir).parts
    ):
        raise ValueError("build_subdir must stay inside the repository")
    seed_accounts = policy.get("seed_accounts")
    if seed_accounts is not None and (
        not isinstance(seed_accounts, list) or len(seed_accounts) > 96
        or any(not isinstance(key, str) or len(from_base58(key)) != 32 for key in seed_accounts)
        or len(set(seed_accounts)) != len(seed_accounts)
    ):
        raise ValueError("seed_accounts must contain at most 96 unique public keys")
    return repo, library, subdir, webhook, None if seed_accounts is None else tuple(seed_accounts)


def customer_policies(raw):
    customers = json.loads(raw)
    if not isinstance(customers, dict):
        raise ValueError("CANARY_CUSTOMERS must be an object keyed by customer ID")
    targets = {}
    for customer, policy in customers.items():
        plan = policy.get("plan", "starter")
        if plan not in PLANS:
            raise ValueError("unknown customer plan")
        monthly, replay = PLANS[plan]
        limit = policy.get("max_upgrades_per_month", policy.get("monthly_proposals", monthly))
        replay = policy.get("max_replay_txs", replay)
        cooldown = policy.get("cooldown_minutes", 0)
        active = policy.get("active", True)
        expires = datetime.fromisoformat(policy.get("active_until", "9999-01-01T00:00:00Z"))
        if (not customer or (limit is None and plan != "enterprise")
                or (limit is not None and (type(limit) is not int or limit < 1))
                or type(replay) is not int or not 1 <= replay <= 5000
                or type(cooldown) is not int or cooldown < 0 or type(active) is not bool
                or expires.utcoffset() is None or not isinstance(policy["targets"], list)):
            raise ValueError("invalid customer policy")
        options = build_options(policy)
        for target in policy["targets"]:
            if not isinstance(target, str) or len(from_base58(target)) != 32 or target in targets:
                raise ValueError("invalid or duplicate target public key")
            targets[target] = Policy(customer, expires, limit, replay, cooldown, active, *options)
    return targets


class Admissions:
    def __init__(self, database, policies, on_notice=None):
        self.database = database
        self.policies = policies
        self.on_notice = on_notice
        database.executescript("""
            CREATE TABLE IF NOT EXISTS admissions (
                gate TEXT NOT NULL,
                proposal TEXT NOT NULL,
                customer TEXT NOT NULL,
                month TEXT NOT NULL,
                PRIMARY KEY (gate, proposal)
            );
            CREATE INDEX IF NOT EXISTS admissions_usage ON admissions(gate, customer, month);
            CREATE TABLE IF NOT EXISTS customer_jobs (
                gate TEXT NOT NULL,
                customer TEXT NOT NULL,
                proposal TEXT NOT NULL,
                payload TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                PRIMARY KEY(gate, proposal)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_customer_job
                ON customer_jobs(gate, customer) WHERE finished_at IS NULL;
            CREATE TABLE IF NOT EXISTS job_outcomes (
                gate TEXT NOT NULL,
                proposal TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                PRIMARY KEY(gate, proposal)
            );
            CREATE TABLE IF NOT EXISTS cursors (gate TEXT PRIMARY KEY, next_slot INTEGER NOT NULL);
        """)

    def consume(self, event, now, enqueue=False):
        policy = self.policies.get(event["target"])
        if policy is None or not policy.active or now >= policy.expires:
            return None
        customer = policy.customer
        month = now.astimezone(timezone.utc).strftime("%Y-%m")
        result = self._consume(event, now, policy, customer, month, enqueue)
        if result is False:
            if self.on_notice is not None:
                self.on_notice("QuotaExceeded", dict(customer=customer, proposal=event["proposal"], month=month))
            return None
        return result

    def _consume(self, event, now, policy, customer, month, enqueue):
        with self.database as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admissions WHERE gate = ? AND proposal = ?",
                          (GATE, event["proposal"])).fetchone():
                return None
            used, = db.execute(
                "SELECT count(*) FROM admissions WHERE gate = ? AND customer = ? AND month = ?",
                (GATE, customer, month),
            ).fetchone()
            if policy.monthly is not None and used >= policy.monthly:
                print(json.dumps({"event": "quota_exceeded", "customer": customer,
                                  "proposal": event["proposal"], "month": month}), flush=True)
                return False
            if enqueue:
                recent = db.execute("""
                    SELECT finished_at FROM customer_jobs WHERE gate = ? AND customer = ?
                    ORDER BY started_at DESC LIMIT 1
                """, (GATE, customer)).fetchone()
                if recent and (recent[0] is None or now.timestamp() < recent[0] + 60 * policy.cooldown_minutes):
                    print(json.dumps({"event": "customer_busy_or_cooldown", "customer": customer,
                                      "proposal": event["proposal"]}), flush=True)
                    return None
                payload = dict(event, max_replay_txs=policy.max_replay_txs, live_prestate=policy.seed_accounts is not None)
                db.execute("INSERT INTO customer_jobs VALUES (?, ?, ?, ?, ?, NULL)",
                           (GATE, customer, event["proposal"], json.dumps(payload), int(now.timestamp())))
            db.execute("INSERT INTO admissions VALUES (?, ?, ?, ?)",
                       (GATE, event["proposal"], customer, month))
        return customer

    def active_jobs(self):
        rows = self.database.execute("""
            SELECT payload FROM customer_jobs WHERE gate = ? AND finished_at IS NULL
            ORDER BY started_at
        """, (GATE,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def finish(self, proposal, now, status="closed", reason=None):
        with self.database as db:
            updated = db.execute("""
                UPDATE customer_jobs SET finished_at = ?
                WHERE gate = ? AND proposal = ? AND finished_at IS NULL
            """, (int(now.timestamp()), GATE, proposal))
            if updated.rowcount != 1:
                return False
            self._record_outcome(db, proposal, status, reason)
            return True

    def _record_outcome(self, db, proposal, status, reason):
        db.execute("""
            INSERT INTO job_outcomes VALUES (?, ?, ?, ?)
            ON CONFLICT(gate, proposal) DO UPDATE
              SET status = excluded.status, reason = excluded.reason
        """, (GATE, proposal, status, reason))

    def record_prestate(self, proposal, slot):
        with self.database as db:
            self._record_outcome(db, proposal, "prestate_captured", f"live state captured at slot {slot}")

    def cursor(self):
        row = self.database.execute("SELECT next_slot FROM cursors WHERE gate = ?", (GATE,)).fetchone()
        return row[0] if row else None

    def checkpoint(self, slot):
        with self.database as db:
            db.execute("""
                INSERT INTO cursors VALUES (?, ?)
                ON CONFLICT(gate) DO UPDATE SET next_slot = max(next_slot, excluded.next_slot)
            """, (GATE, slot))


def capture_ready(admissions, snapshots, next_slot, now, on_notice=None, live_jobs=()):
    """Collect finalized traffic once a trusted pre-state is available."""
    for event in admissions.active_jobs():
        if event["anchor_slot"] + event["replay_slots"] >= next_slot:
            continue
        if event["proposal"] in live_jobs:
            continue
        policy = admissions.policies.get(event["target"])
        if policy is None or not policy.active or now >= policy.expires:
            admissions.finish(event["proposal"], now, "refused", "customer inactive")
            continue
        if len(from_base58(event["proposal"])) != 32:
            raise ValueError("invalid proposal address")
        directory = snapshots / event["proposal"]
        snapshot_path = directory / "snapshot.json"
        archive_path = directory / "archive.json"
        if not snapshot_path.is_file() or archive_path.exists():
            continue
        try:
            with snapshot_path.open("rb") as snapshot_file:
                data = snapshot_file.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("snapshot exceeds 8 MiB")
            snapshot = json.loads(data)
            if (to_base58(bytes(snapshot["target"])) != event["target"]
                    or any(snapshot.get(field) != event[field] for field in ("anchor_slot", "replay_slots", "nonce"))):
                raise ValueError("snapshot does not match proposal event")
            limit = min(policy.max_replay_txs, event["max_replay_txs"])
            archive = capture(snapshot, limit)
            staged = directory / ".archive.json.tmp"
            staged.write_text(json.dumps(archive, separators=(",", ":")), encoding="utf-8")
            staged.replace(archive_path)
            print(json.dumps({"event": "archive_captured", "proposal": event["proposal"],
                              "transactions": len(archive["records"])}), flush=True)
        except UnsupportedReplayVersion as exc:
            admissions.finish(event["proposal"], now, "refused", str(exc))
            if on_notice is not None:
                on_notice("ReplayFailed", dict(proposal=event["proposal"], reason=str(exc)))
            print(json.dumps({"event": "capture_refused", "proposal": event["proposal"],
                              "reason": str(exc)}), flush=True)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            # Keep the job and allow the operator to repair its trusted inputs.
            print(json.dumps({"event": "capture_failed", "proposal": event["proposal"],
                              "error": type(exc).__name__}), flush=True)
        # Leave time for event ingestion even when many snapshots are ready.
        break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="/data/admissions.sqlite3")
    parser.add_argument("--customers", type=Path, default=Path("/data/customers.json"))
    parser.add_argument("--snapshots", type=Path, default=Path("/data/snapshots"))
    parser.add_argument("--finish", metavar="PROPOSAL")
    args = parser.parse_args()
    if args.finish:
        with closing(sqlite3.connect(args.database)) as database:
            if not Admissions(database, {}).finish(args.finish, datetime.now(timezone.utc)):
                raise SystemExit("no active job matches proposal")
        return
    def load_policies():
        raw = (args.customers.read_text(encoding="utf-8") if args.customers.exists()
               else os.environ["CANARY_CUSTOMERS"])
        return customer_policies(raw)

    policies = load_policies()
    if rpc("getGenesisHash") != GENESIS:
        raise ValueError("expected Solana Devnet")
    with closing(sqlite3.connect(args.database)) as database:
        admissions = Admissions(database, policies, notify)
        live_jobs = {}
        # Time-sensitive live snapshots cannot be recovered from the disk after a restart.
        for job in admissions.active_jobs():
            if job.get("live_prestate") or (policies.get(job["target"]) and policies[job["target"]].seed_accounts is not None):
                admissions.finish(job["proposal"], datetime.now(timezone.utc),
                                  "refused", "live pre-state was lost on restart")

        def refresh_policies():
            admissions.policies = load_policies()

        def on_event(event):
            refresh_policies()
            customer = admissions.consume(event, datetime.now(timezone.utc), enqueue=True)
            if customer is not None:
                policy = admissions.policies[event["target"]]
                if policy.seed_accounts is not None:
                    proposal = event["proposal"]
                    try:
                        bundle = capture_prestate(event, policy.seed_accounts)
                        if sum(b["encoded_bytes"] for b in live_jobs.values()) + bundle["encoded_bytes"] > 16 * 1024 * 1024:
                            raise PrestateRefused("live pre-state cache is full")
                        live_jobs[proposal] = bundle
                        admissions.record_prestate(proposal, bundle["capture_slot"])
                    except PrestateRefused as exc:
                        admissions.finish(proposal, datetime.now(timezone.utc), "refused", str(exc))
                        notify("ReplayFailed", dict(proposal=proposal, reason=str(exc)))
                        return
                    except (OSError, KeyError, RuntimeError, TypeError, ValueError):
                        reason = "unable to read consistent live pre-state"
                        admissions.finish(proposal, datetime.now(timezone.utc), "refused", reason)
                        notify("ReplayFailed", dict(proposal=proposal, reason=reason))
                        return
                print(json.dumps(dict(event, customer=customer)), flush=True)
                notify("ProposalCreated", dict(event, customer=customer))

        def on_lifecycle(name, event):
            refresh_policies()
            if event["target"] in admissions.policies:
                if name == "UpgradeExecuted":
                    live_jobs.pop(event["proposal"], None)
                    admissions.finish(event["proposal"], datetime.now(timezone.utc), "executed")
                notify(name, event)

        next_capture = 0
        balance_check = 0
        low_balance = False
        verifier_address = os.environ.get("CANARY_VERIFIER_PUBKEY")

        def checkpoint(slot):
            nonlocal next_capture, balance_check, low_balance
            admissions.checkpoint(slot)
            if time.monotonic() >= next_capture:
                refresh_policies()
                now = datetime.now(timezone.utc)
                for proposal, bundle in list(live_jobs.items()):
                    if now.timestamp() > bundle["expires_at"]:
                        live_jobs.pop(proposal)
                        admissions.finish(proposal, now, "refused", "proposal expired")
                capture_ready(admissions, args.snapshots, slot, now, notify, live_jobs)
                next_capture = time.monotonic() + 30
            if verifier_address and time.monotonic() >= balance_check:
                balance_check = time.monotonic() + 300
                try:
                    balance = rpc("getBalance", verifier_address, {"commitment": "finalized"})["value"]
                    if balance < 50_000_000 and not low_balance:
                        notify("VerifierLowBalance", {"verifier": verifier_address, "lamports": balance})
                    low_balance = balance < 50_000_000
                except (OSError, RuntimeError, KeyError):
                    print('{"event":"balance_check_failed"}', flush=True)

        wake = Event()
        server = start_http(args.database, GATE, load_policies, wake)
        initial_cursor = admissions.cursor()
        if initial_cursor is None:
            # Cover webhook traffic that arrived while this Machine was starting.
            initial_cursor = max(0, rpc("getSlot", {"commitment": "finalized"}) - 256)
            admissions.checkpoint(initial_cursor)

        def wait(seconds):
            wake.wait(seconds)
            wake.clear()

        try:
            watch(initial_cursor, on_event=on_event, checkpoint=checkpoint, wait=wait, on_lifecycle=on_lifecycle)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
