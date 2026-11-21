"""Admission policy and durable monthly usage for the Fly Devnet listener."""
import argparse
import json
import os
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from devnet import GENESIS, from_base58, to_base58
from live_capture import GATE, UnsupportedReplayVersion, capture, rpc, watch
from live_prestate import PrestateRefused, capture_prestate, replay_archive
from worker_build import FEATURE, BuildFailed, build_candidate, remote_build
from worker_http import start_http
from worker_notify import notify
from worker_vote import (CandidateRejected, ReplayIncomplete, VoteRefused, attest, discard_job_files, finalized_vote,
                         load_keypair)

MAX_BUILD_ATTEMPTS = 3


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
    build_features: tuple[str, ...] = ()


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
    features = policy.get("build_features", [])
    if (not isinstance(features, list) or len(features) > 16
            or any(not isinstance(f, str) or not FEATURE.fullmatch(f) for f in features)):
        raise ValueError("build_features must list at most 16 cargo feature names")
    return (repo, library, subdir, webhook, None if seed_accounts is None else tuple(seed_accounts),
            tuple(features))


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

    def record_traffic_preflight(self, proposal, count):
        with self.database as db:
            self._record_outcome(db, proposal, "traffic_preflight_passed",
                                 f"{count} finalized target transactions checked for account coverage and unrelated writes")

    def record_executed(self, proposal):
        """Keep the vote record and mark that the gate later executed the upgrade."""
        with self.database as db:
            db.execute("""
                UPDATE job_outcomes SET status = 'executed', reason = coalesce(reason || '; ', '') || 'upgrade executed'
                WHERE gate = ? AND proposal = ? AND status IN ('voted', 'verified')
            """, (GATE, proposal))

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
            archive = capture(snapshot, limit, include_baseline=True)
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


@dataclass(frozen=True)
class Attestor:
    """Build -> verify -> vote for one proposal, using this node's own tools and key."""
    runner: str
    toolchain: str
    jobs: Path
    keypair: object
    gate: str
    keep_files: bool = False
    load_policies: object = None
    pending_cleanup: set = field(default_factory=set, compare=False)

    def build(self, policy, commit, work):
        if host := os.environ.get("CANARY_BUILDER_HOST"):
            return remote_build(host, policy, commit, work)
        return build_candidate(policy.repo_url, commit, policy.build_subdir or ".", policy.library_name,
                               policy.build_features, work, self.toolchain)

    def __call__(self, event, bundle, policy):
        def authorize():
            now = datetime.now(timezone.utc)
            current = self.load_policies().get(event["target"]) if self.load_policies else policy
            if (bundle.get("cancelled") or current != policy or not current.active
                    or now >= current.expires or now.timestamp() > bundle["expires_at"]):
                raise VoteRefused("proposal or customer authorization expired or changed")
        return attest(event, bundle, policy, self.jobs / event["proposal"], self.runner, self.build,
                      self.keypair, self.gate, authorize)

    def discard(self, proposal):
        """Customer state lives on disk only while canary-gate needs to read it."""
        if not self.keep_files and len(from_base58(proposal)) == 32:
            try:
                discard_job_files(self.jobs / proposal)
            except OSError as exc:
                self.pending_cleanup.add(proposal)
                print(json.dumps(dict(event="cleanup_failed", proposal=proposal, error=type(exc).__name__)), flush=True)
            else:
                self.pending_cleanup.discard(proposal)

    def discard_all(self):
        if not self.keep_files and self.jobs.is_dir():
            for directory in self.jobs.iterdir():
                if directory.is_dir():
                    self.discard(directory.name)


class InlineExecutor:
    """Runs submitted work immediately. Tests and one-shot tools use it."""

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def attest_job(event, bundle, policy, attestor):
    """Return (status, reason, notice) for a final outcome, or None to retry later."""
    proposal = event["proposal"]
    if policy.repo_url is None or policy.library_name is None:
        return "refused", "customer build source is not configured", "ReplayFailed"
    try:
        result = attestor(event, bundle, policy)
    except BuildFailed as exc:
        bundle["build_attempts"] = bundle.get("build_attempts", 0) + 1
        if bundle["build_attempts"] < MAX_BUILD_ATTEMPTS:
            print(json.dumps(dict(event="candidate_build_retry", proposal=proposal, error=str(exc)[:300])), flush=True)
            return None
        return "refused", f"candidate build failed: {str(exc)[:300]}", "ReplayFailed"
    except CandidateRejected as exc:
        return "rejected", str(exc), "CandidateRejected"
    except ReplayIncomplete as exc:
        # The replay could not judge the candidate. Report it as missing evidence.
        return "refused", f"replay evidence incomplete: {exc}", "ReplayFailed"
    except VoteRefused as exc:
        return "refused", f"vote refused: {exc}", "ReplayFailed"
    except (OSError, RuntimeError) as exc:
        print(json.dumps(dict(event="attestation_retry", proposal=proposal, error=type(exc).__name__)), flush=True)
        return None
    except (ValueError, KeyError, TypeError):
        return "refused", "invalid verifier evidence", "ReplayFailed"
    details = dict(proposal=proposal, **result)
    print(json.dumps(dict(details, event="attestation_" + result["status"])), flush=True)
    if result["status"] == "voted":
        reason = f"vote {result['signature'] or 'already recorded'}; diff_root {result['diff_root']}"
        return "voted", reason, ("VoteSubmitted", details)
    return "verified", f"diff_root {result['diff_root']}; not a configured verifier", ("ReplayVerified", details)


def verify_job(event, bundle, policy, limit, attestor):
    """Runs off the event loop. Returns (preflight count or None, final outcome or None)."""
    count = None
    if "archive" not in bundle:
        try:
            archive = replay_archive(event, bundle, limit)
        except (OSError, RuntimeError):
            print(json.dumps(dict(event="traffic_preflight_retry", proposal=event["proposal"])), flush=True)
            return None, None
        except ValueError as exc:
            return None, ("refused", str(exc), "ReplayFailed")
        except (KeyError, TypeError):
            return None, ("refused", "invalid finalized transaction metadata", "ReplayFailed")
        count = len(archive["records"])
        bundle["archive"] = archive
        bundle["transaction_count"] = count
    if attestor is None:
        return count, None
    return count, attest_job(event, bundle, policy, attestor)


EXECUTED_EARLY = "upgrade executed before this node finished verification"


def observe_upgrade(admissions, live_jobs, running, attestor, proposal, now):
    """Close a job once the gate executed its upgrade.

    A vote may still be finalizing in the worker thread. Then the job stays open
    so its outcome is recorded first, and is marked executed afterwards.
    """
    bundle = live_jobs.get(proposal)
    if bundle is not None and proposal in running:
        bundle["executed"] = True
        return
    live_jobs.pop(proposal, None)
    if attestor is not None:
        attestor.discard(proposal)
    if not admissions.finish(proposal, now, "executed", EXECUTED_EARLY):
        admissions.record_executed(proposal)


def check_live_traffic(admissions, live_jobs, next_slot, now, on_notice, attestor=None, executor=None, running=None):
    """Start, collect and close verification jobs. Never waits for a job to finish.

    With a thread pool as `executor`, replay capture, builds and vote finalization
    run beside the event loop, so new proposals are still captured within the
    anchor drift limit.
    """
    executor = InlineExecutor() if executor is None else executor
    running = {} if running is None else running
    for proposal in [p for p in running if p not in live_jobs]:
        running.pop(proposal)
    for event in admissions.active_jobs():
        proposal = event["proposal"]
        bundle = live_jobs.get(proposal)
        if bundle is None:
            continue
        policy = admissions.policies.get(event["target"])
        outcome = None
        if policy is None or not policy.active or now >= policy.expires:
            outcome = "refused", "customer inactive", "ReplayFailed"
        elif now.timestamp() > bundle["expires_at"]:
            outcome = "refused", "proposal expired", "ReplayFailed"
        if outcome is not None and proposal in running:
            bundle["cancelled"] = True
            task = running[proposal]
            if not task.cancel():
                if not task.done():
                    continue
                try:
                    _, completed = task.result()
                    if completed and completed[0] == "voted":
                        outcome = completed
                except Exception as exc:
                    print(json.dumps(dict(event="cancelled_job_error", proposal=proposal,
                                          error=type(exc).__name__)), flush=True)
        if outcome is None:
            finalized = event["anchor_slot"] + event["replay_slots"] < next_slot
            pending = "archive" not in bundle or attestor is not None
            if proposal not in running and finalized and pending:
                limit = min(policy.max_replay_txs, event["max_replay_txs"])
                running[proposal] = executor.submit(verify_job, event, bundle, policy, limit, attestor)
            task = running.get(proposal)
            if task is not None and task.done():
                running.pop(proposal)
                try:
                    count, outcome = task.result()
                except Exception as exc:  # A bug in one job must not stop the listener.
                    print(json.dumps(dict(event="verification_error", proposal=proposal,
                                          error=f"{type(exc).__name__}: {str(exc)[:200]}")), flush=True)
                    count = None
                if count is not None:
                    admissions.record_traffic_preflight(proposal, count)
                if outcome is None and bundle.get("executed"):
                    outcome = "executed", EXECUTED_EARLY, None
        if outcome is not None:
            status, reason, notice = outcome
            admissions.finish(proposal, now, status, reason)
            if bundle.get("executed"):
                admissions.record_executed(proposal)
            live_jobs.pop(proposal)
            running.pop(proposal, None)
            if attestor is not None:
                attestor.discard(proposal)
            if isinstance(notice, tuple):
                on_notice(*notice)
            elif notice is not None:
                on_notice(notice, dict(proposal=proposal, reason=reason))


LIVE_CACHE_BYTES = 16 * 1024 * 1024


def collect_captures(admissions, captures, live_jobs, now, on_notice):
    """Move finished live pre-state captures into the cache, or refuse their jobs."""
    for proposal, future in list(captures.items()):
        if not future.done():
            continue
        del captures[proposal]
        try:
            bundle = future.result()
            if sum(b["encoded_bytes"] for b in live_jobs.values()) + bundle["encoded_bytes"] > LIVE_CACHE_BYTES:
                raise PrestateRefused("live pre-state cache is full")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PrestateRefused) else "unable to read consistent live pre-state"
            if admissions.finish(proposal, now, "refused", reason):
                on_notice("ReplayFailed", dict(proposal=proposal, reason=reason))
            continue
        if proposal not in {job["proposal"] for job in admissions.active_jobs()}:
            # The job closed while its capture ran. Nothing would ever release the state.
            continue
        live_jobs[proposal] = bundle
        admissions.record_prestate(proposal, bundle["capture_slot"])


def reconcile_votes(admissions, interrupted_votes, now, on_notice, gate=GATE):
    """Settle votes that may have been sent before a restart, from finalized chain state.

    `interrupted_votes` maps a proposal to (job, receipt), where the receipt is the
    vote-intent.json written just before the vote was sent. A job stays open while
    its vote can still land.
    """
    for proposal, (job, receipt) in list(interrupted_votes.items()):
        try:
            evidence = receipt["evidence"]
            if finalized_vote(job, receipt, evidence, receipt["verifier"], gate):
                root = bytes(evidence["diff_root"]).hex()
                admissions.finish(proposal, now, "voted", f"vote confirmed on chain; diff_root {root}")
                on_notice("VoteSubmitted", dict(proposal=proposal, verifier=receipt["verifier"], diff_root=root))
            elif rpc("getBlockHeight", {"commitment": "finalized"}) > receipt["last_height"]:
                # Re-read after the finalized expiry height in case the vote landed between reads.
                if finalized_vote(job, receipt, evidence, receipt["verifier"], gate):
                    continue
                admissions.finish(proposal, now, "refused", "interrupted vote expired without confirmation")
            else:
                continue
        except (RuntimeError, OSError):
            continue
        except (KeyError, ValueError, TypeError) as exc:
            admissions.finish(proposal, now, "refused", f"vote reconciliation failed: {exc}")
        if receipt.get("executed"):
            admissions.record_executed(proposal)
        del interrupted_votes[proposal]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="/data/admissions.sqlite3")
    parser.add_argument("--customers", type=Path, default=Path("/data/customers.json"))
    parser.add_argument("--snapshots", type=Path, default=Path("/data/snapshots"))
    parser.add_argument("--jobs", type=Path, default=Path("/data/jobs"))
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
    # A loopback endpoint is a disposable validator used for the rehearsal.
    local = urlsplit(os.environ["RPC_URL"]).hostname in ("127.0.0.1", "localhost")
    if not local and rpc("getGenesisHash") != GENESIS:
        raise ValueError("expected Solana Devnet")
    keypair = load_keypair()
    runner, toolchain = os.environ.get("CANARY_GATE_BIN"), os.environ.get("CANARY_SBF_TOOLCHAIN")
    tools = toolchain or os.environ.get("CANARY_PLATFORM_TOOLS") or os.environ.get("CANARY_BUILDER_HOST")
    keep = os.environ.get("CANARY_KEEP_JOB_FILES") == "1"
    attestor = Attestor(runner, toolchain, args.jobs, keypair, GATE, keep, load_policies) if runner and tools else None
    if attestor is not None:
        # Nothing survives a restart, so no captured state may either.
        attestor.discard_all()
    # One job at a time beside the event loop. Builds need the Machine's memory.
    executor, running = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify"), {}
    print(json.dumps(dict(event="worker_started", gate=GATE, attestation=attestor is not None,
                          verifier=keypair.address if keypair else None)), flush=True)
    with closing(sqlite3.connect(args.database)) as database:
        admissions = Admissions(database, policies, notify)
        live_jobs = {}
        interrupted_votes = {}
        # Time-sensitive live snapshots cannot be recovered from the disk after a restart.
        for job in admissions.active_jobs():
            intent = args.jobs / job["proposal"] / "vote-intent.json"
            if attestor and intent.is_file():
                interrupted_votes[job["proposal"]] = (job, json.loads(intent.read_bytes()))
                continue
            if job.get("live_prestate") or (policies.get(job["target"]) and policies[job["target"]].seed_accounts is not None):
                admissions.finish(job["proposal"], datetime.now(timezone.utc),
                                  "refused", "live pre-state was lost on restart")

        captures = {}
        capture_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prestate")

        def refresh_policies():
            admissions.policies = load_policies()

        def on_event(event):
            refresh_policies()
            customer = admissions.consume(event, datetime.now(timezone.utc), enqueue=True)
            if customer is not None:
                policy = admissions.policies[event["target"]]
                if policy.seed_accounts is not None:
                    proposal = event["proposal"]
                    if len(captures) >= 16:
                        reason = "live capture queue is full"
                        admissions.finish(proposal, datetime.now(timezone.utc), "refused", reason)
                        notify("ReplayFailed", dict(proposal=proposal, reason=reason))
                        return
                    captures[proposal] = capture_pool.submit(capture_prestate, event, policy.seed_accounts)
                print(json.dumps(dict(event, customer=customer)), flush=True)
                notify("ProposalCreated", dict(event, customer=customer))

        def on_lifecycle(name, event):
            refresh_policies()
            if event["target"] in admissions.policies:
                if name == "UpgradeExecuted":
                    capture_task = captures.pop(event["proposal"], None)
                    if capture_task is not None:
                        capture_task.cancel()
                    if event["proposal"] in interrupted_votes:
                        interrupted_votes[event["proposal"]][1]["executed"] = True
                    else:
                        observe_upgrade(admissions, live_jobs, running, attestor, event["proposal"],
                                        datetime.now(timezone.utc))
                notify(name, event)

        next_capture = 0
        last_checkpoint = 0
        balance_check = 0
        low_balance = False
        verifier_address = os.environ.get("CANARY_VERIFIER_PUBKEY") or (keypair.address if keypair else None)

        def checkpoint(slot):
            nonlocal next_capture, balance_check, low_balance, last_checkpoint
            admissions.checkpoint(slot)
            last_checkpoint = time.monotonic()
            collect_captures(admissions, captures, live_jobs, datetime.now(timezone.utc), notify)
            if time.monotonic() >= next_capture:
                refresh_policies()
                if attestor is not None:
                    for proposal in list(attestor.pending_cleanup):
                        attestor.discard(proposal)
                now = datetime.now(timezone.utc)
                reconcile_votes(admissions, interrupted_votes, now, notify)
                check_live_traffic(admissions, live_jobs, slot, now, notify, attestor, executor, running)
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
        def ready():
            return bool(last_checkpoint and time.monotonic() - last_checkpoint < 180
                        and not (attestor and attestor.pending_cleanup))

        server = start_http(args.database, GATE, load_policies, wake, ready=ready)
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
            capture_pool.shutdown(wait=False, cancel_futures=True)
            executor.shutdown(wait=False, cancel_futures=True)
            if server is not None:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
