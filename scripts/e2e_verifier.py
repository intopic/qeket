"""Start one independent verifier worker for the counter cycle, or wait for its outcome."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FINAL_STATUSES = {"voted", "executed", "verified", "refused", "rejected"}


def start(args):
    sys.path.insert(0, str(ROOT / "scripts"))
    from live_cycle import PRIVATE, key
    work = args.workdir.resolve()
    (work / "jobs").mkdir(parents=True, exist_ok=True)
    # A remote verifier keeps only its own vote key and receives public addresses.
    target = os.environ.get("CANARY_E2E_TARGET") or str(key("target").pubkey())
    gate = os.environ.get("GATE_PROGRAM_ID") or str(key("gate").pubkey())
    customers = {"counter-e2e": {
        "targets": [target], "plan": "starter",
        "repo_url": args.repo_url, "build_subdir": "examples/counter", "library_name": "canary_counter",
        "build_features": ["candidate"],
    }}
    (work / "customers.json").write_text(json.dumps(customers, indent=2), encoding="utf-8")
    env = dict(os.environ, GATE_PROGRAM_ID=gate, PYTHONPATH=str(ROOT / "scripts"),
               CANARY_VERIFIER_KEYPAIR_FILE=str(PRIVATE / args.key),
               CANARY_GATE_BIN=str((ROOT / "target/debug/canary-gate").resolve()),
               # Test runs keep archives as CI artifacts. Production workers delete them.
               CANARY_KEEP_JOB_FILES="1")
    env.pop("CANARY_HTTP_PORT", None)
    log = (work / "worker.log").open("ab")
    process = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "scripts/fly_listener.py"), "--database", str(work / "admissions.sqlite3"),
         "--customers", str(work / "customers.json"), "--jobs", str(work / "jobs")],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (work / "worker.pid").write_text(str(process.pid))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("worker exited: " + (work / "worker.log").read_text(errors="replace")[-2000:])
        for line in (work / "worker.log").read_text(errors="replace").splitlines():
            if '"worker_started"' in line:
                started = json.loads(line)
                if not started["attestation"] or started["verifier"] != str(key(args.key).pubkey()):
                    raise SystemExit("worker started without attestation: " + line)
                print(line, flush=True)
                return
        time.sleep(1)
    raise SystemExit("worker did not start")


def outcome(work):
    database = work / "admissions.sqlite3"
    if not database.exists():
        return None
    with sqlite3.connect(database) as db:
        try:
            return db.execute("SELECT proposal, status, reason FROM job_outcomes").fetchone()
        except sqlite3.OperationalError:
            return None


def wait(args):
    work = args.workdir.resolve()
    deadline = time.monotonic() + args.timeout
    want = set(args.until.split(","))
    while time.monotonic() < deadline:
        row = outcome(work)
        if row and row[1] in FINAL_STATUSES and (row[1] in want or row[1] in ("refused", "rejected")):
            evidence = next(work.glob("jobs/*/evidence.json"), None)
            print(json.dumps(dict(proposal=row[0], status=row[1], reason=row[2],
                                  evidence=json.loads(evidence.read_text()) if evidence else None)), flush=True)
            raise SystemExit(0 if row[1] in want else 1)
        time.sleep(5)
    print(json.dumps(dict(status="timeout", last=outcome(work))), flush=True)
    raise SystemExit(1)


def stop(args):
    pid = args.workdir.resolve() / "worker.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 15)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    starter = sub.add_parser("start")
    starter.add_argument("--key", required=True, choices=["vote0", "vote1", "vote2"])
    starter.add_argument("--repo-url", default=os.environ.get("CANARY_REPO_URL", "https://github.com/intopic/qeket"))
    waiter = sub.add_parser("wait")
    waiter.add_argument("--timeout", type=int, default=2400)
    waiter.add_argument("--until", default="voted,executed")
    stopper = sub.add_parser("stop")
    for command in (starter, waiter, stopper):
        command.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    {"start": start, "wait": wait, "stop": stop}[args.action](args)
