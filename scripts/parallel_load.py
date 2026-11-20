"""Run three independent fixture builds and replays on one Linux host."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
from threading import Barrier
import time

from mvp import ROOT, OUT, build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sbf-toolchain", required=True)
    args = parser.parse_args()
    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    gate = ROOT / "target/sbpf-solana-solana/release/canary_upgrade_gate.so"
    runner = ROOT / "target/debug/canary-gate"
    regression = OUT / "regression/sbpf-solana-solana/release/canary_counter.so"
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    build_ready = Barrier(3, timeout=180)
    replay_ready = Barrier(3, timeout=480)
    measurement_start = time.monotonic()

    def elapsed():
        return time.monotonic() - measurement_start

    def client(number):
        directory = work / f"client-{number}"
        source = directory / "counter"
        shutil.copytree(ROOT / "examples/counter", source, ignore=shutil.ignore_patterns("target"))
        manifest = source / "Cargo.toml"
        evidence = directory / "evidence"
        build_ready.wait()
        start_build = elapsed()
        baseline = build(manifest, "canary_counter", directory / "baseline", args.sbf_toolchain)
        candidate = build(manifest, "canary_counter", directory / "candidate", args.sbf_toolchain, "candidate")
        rebuilt = build(manifest, "canary_counter", directory / "rebuild", args.sbf_toolchain, "candidate")
        stop_build = elapsed()
        if candidate.read_bytes() != rebuilt.read_bytes():
            raise ValueError(f"client {number} candidate and rebuild differ")
        replay_ready.wait()
        start_replay = elapsed()
        command = [runner, "demo", gate, baseline, candidate, rebuilt, version, evidence, regression]
        with (directory / "replay.log").open("wb") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        stop_replay = elapsed()
        result = json.loads((evidence / "report.json").read_bytes())
        if result["replayed_transactions_per_verifier"] != 1000 or result["late_regression"]["expected_transaction_ordinal"] != 901:
            raise ValueError(f"unexpected coverage for client {number}")
        return {
            "client": number,
            "build_interval_seconds": [start_build, stop_build],
            "replay_interval_seconds": [start_replay, stop_replay],
            "replay_per_verifier_ms": result["replay_per_verifier_ms"],
            "transactions_per_verifier": result["replayed_transactions_per_verifier"],
            "v0_alt_cpi_transactions": result["v0_alt_cpi_transactions"],
            "local_fault_checks": result["local_fault_checks"],
            "late_regression_ordinal": result["late_regression"]["expected_transaction_ordinal"],
            "archive_sha256": result["archive_sha256"],
        }

    def run_client(number):
        try:
            return client(number)
        except BaseException:
            build_ready.abort()
            replay_ready.abort()
            raise

    with ThreadPoolExecutor(max_workers=3) as executor:
        clients = list(executor.map(run_client, range(1, 4)))
    if len({client["archive_sha256"] for client in clients}) != 3:
        raise RuntimeError("client archives must be distinct")
    output = {"fixture": "counter", "source_commit": version, "clients": clients}
    for phase in ("build", "replay"):
        intervals = [client[f"{phase}_interval_seconds"] for client in clients]
        overlap = min(end for _, end in intervals) - max(start for start, _ in intervals)
        if overlap <= 0:
            raise RuntimeError(f"{phase} jobs did not overlap")
        output[f"{phase}_overlap_seconds"] = overlap
    (work / "parallel-load.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
