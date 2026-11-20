"""Admission policy and durable monthly usage for the Fly Devnet listener."""
import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from devnet import GENESIS, from_base58
from live_capture import GATE, rpc, watch


PLANS = {"starter": (5, 500), "pro": (20, 2000), "enterprise": (None, 5000)}


@dataclass(frozen=True)
class Policy:
    customer: str
    expires: datetime
    monthly: int | None
    max_replay_txs: int
    cooldown_minutes: int
    active: bool


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
        for target in policy["targets"]:
            if not isinstance(target, str) or len(from_base58(target)) != 32 or target in targets:
                raise ValueError("invalid or duplicate target public key")
            targets[target] = Policy(customer, expires, limit, replay, cooldown, active)
    return targets


class Admissions:
    def __init__(self, database, policies):
        self.database = database
        self.policies = policies
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
            CREATE TABLE IF NOT EXISTS cursors (gate TEXT PRIMARY KEY, next_slot INTEGER NOT NULL);
        """)

    def consume(self, event, now, enqueue=False):
        policy = self.policies.get(event["target"])
        if policy is None or not policy.active or now >= policy.expires:
            return None
        customer = policy.customer
        month = now.astimezone(timezone.utc).strftime("%Y-%m")
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
                return None
            if enqueue:
                recent = db.execute("""
                    SELECT finished_at FROM customer_jobs WHERE gate = ? AND customer = ?
                    ORDER BY started_at DESC LIMIT 1
                """, (GATE, customer)).fetchone()
                if recent and (recent[0] is None or now.timestamp() < recent[0] + 60 * policy.cooldown_minutes):
                    print(json.dumps({"event": "customer_busy_or_cooldown", "customer": customer,
                                      "proposal": event["proposal"]}), flush=True)
                    return None
                payload = dict(event, max_replay_txs=policy.max_replay_txs)
                db.execute("INSERT INTO customer_jobs VALUES (?, ?, ?, ?, ?, NULL)",
                           (GATE, customer, event["proposal"], json.dumps(payload), int(now.timestamp())))
            db.execute("INSERT INTO admissions VALUES (?, ?, ?, ?)",
                       (GATE, event["proposal"], customer, month))
        return customer

    def finish(self, proposal, now):
        with self.database as db:
            updated = db.execute("""
                UPDATE customer_jobs SET finished_at = ?
                WHERE gate = ? AND proposal = ? AND finished_at IS NULL
            """, (int(now.timestamp()), GATE, proposal))
            return updated.rowcount == 1

    def cursor(self):
        row = self.database.execute("SELECT next_slot FROM cursors WHERE gate = ?", (GATE,)).fetchone()
        return row[0] if row else None

    def checkpoint(self, slot):
        with self.database as db:
            db.execute("""
                INSERT INTO cursors VALUES (?, ?)
                ON CONFLICT(gate) DO UPDATE SET next_slot = max(next_slot, excluded.next_slot)
            """, (GATE, slot))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="/data/admissions.sqlite3")
    parser.add_argument("--customers", type=Path, default=Path("/data/customers.json"))
    parser.add_argument("--finish", metavar="PROPOSAL")
    args = parser.parse_args()
    if args.finish:
        with closing(sqlite3.connect(args.database)) as database:
            if not Admissions(database, {}).finish(args.finish, datetime.now(timezone.utc)):
                raise SystemExit("no active job matches proposal")
        return
    policies = customer_policies(args.customers.read_text(encoding="utf-8") if args.customers.exists()
                                 else os.environ["CANARY_CUSTOMERS"])
    if rpc("getGenesisHash") != GENESIS:
        raise ValueError("expected Solana Devnet")
    with closing(sqlite3.connect(args.database)) as database:
        admissions = Admissions(database, policies)

        def on_event(event):
            if args.customers.exists():
                admissions.policies = customer_policies(args.customers.read_text(encoding="utf-8"))
            customer = admissions.consume(event, datetime.now(timezone.utc), enqueue=True)
            if customer is not None:
                print(json.dumps(dict(event, customer=customer)), flush=True)

        watch(admissions.cursor(), on_event=on_event, checkpoint=admissions.checkpoint)


if __name__ == "__main__":
    main()
