"""Build the trusted local example twice, then exercise the complete gate in LiteSVM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "target" / "mvp"
FILES = ["Cargo.toml", "Cargo.lock", "src/lib.rs"]


def run(args, cwd=ROOT, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def snapshot():
    repo = OUT / "source"
    repo.mkdir(parents=True, exist_ok=True)
    hooks = OUT / "empty-hooks"
    hooks.mkdir(exist_ok=True)
    git = ["git", "-c", "core.autocrlf=false", "-c", f"core.hooksPath={hooks}",
           "-c", "commit.gpgSign=false", "-c", "user.name=Canary local fixture",
           "-c", "user.email=fixture@localhost"]
    run(git + ["init", "--quiet"], repo)
    for name in FILES:
        (repo / name).parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "examples/counter" / name, repo / name)
    run(git + ["add", "--", *FILES], repo)
    changed = subprocess.run(git + ["diff", "--cached", "--quiet"], cwd=repo).returncode
    if changed == 1:
        run(git + ["commit", "--quiet", "-m", "Local counter source"], repo,
            env=dict(os.environ, GIT_AUTHOR_DATE="2025-01-01T00:00:00Z", GIT_COMMITTER_DATE="2025-01-01T00:00:00Z"))
    elif changed:
        raise RuntimeError("Could not inspect the local fixture repository")
    commit = run(git + ["rev-parse", "HEAD"], repo, capture_output=True, text=True).stdout.strip()
    for worker in ["builder", "verifier"]:
        for name in FILES:
            dest = OUT / worker / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = run(git + ["show", f"{commit}:{name}"], repo, capture_output=True).stdout
            if not dest.exists() or dest.read_bytes() != blob:
                dest.write_bytes(blob)
    return commit


def build(manifest, crate, directory, toolchain, features=None):
    env = dict(os.environ)
    env["CARGO_NET_OFFLINE"] = "true"
    if toolchain:
        args = ["cargo", "+" + toolchain, "build", "--manifest-path", str(manifest), "--target",
                "sbpf-solana-solana", "--target-dir", str(directory), "--release", "--locked"]
        env["CARGO_TARGET_SBPF_SOLANA_SOLANA_RUSTFLAGS"] = f"--remap-path-prefix={manifest.parent}=fixture"
        if features:
            args += ["--features", features]
        run(args, env=env)
        return directory / "sbpf-solana-solana/release" / (crate + ".so")
    args = ["cargo", "build-sbf", "--tools-version", "v1.58", "--manifest-path", str(manifest), "--sbf-out-dir", str(directory / "deploy")]
    if features:
        args += ["--features", features]
    args += ["--", "--locked"]
    run(args, env=dict(env, CARGO_TARGET_DIR=str(directory)))
    return directory / "deploy" / (crate + ".so")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--build-only", action="store_true")
    modes.add_argument("--load-test", action="store_true")
    parser.add_argument("--sbf-toolchain", default=os.environ.get("CANARY_SBF_TOOLCHAIN"))
    args = parser.parse_args()
    toolchain = args.sbf_toolchain
    if not toolchain and not shutil.which("cargo-build-sbf"):
        installed = run(["rustup", "toolchain", "list"], capture_output=True, text=True).stdout
        toolchain = next((s.split()[0] for s in installed.splitlines() if s.split()[0].endswith("-sbpf-solana-v1.58")), None)
        if not toolchain:
            parser.error("Install cargo-build-sbf or specify a linked Solana Rust toolchain")
    commit = snapshot()
    gate = build(ROOT / "programs/canary_upgrade_gate/Cargo.toml", "canary_upgrade_gate", ROOT / "target", toolchain)
    v1 = build(OUT / "builder/Cargo.toml", "canary_counter", OUT / "baseline", toolchain)
    started = perf_counter()
    v2 = build(OUT / "builder/Cargo.toml", "canary_counter", OUT / "candidate", toolchain, "candidate")
    candidate_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    rebuilt = build(OUT / "verifier/Cargo.toml", "canary_counter", OUT / "rebuild", toolchain, "candidate")
    rebuild_ms = (perf_counter() - started) * 1000
    if v1.read_bytes() == v2.read_bytes() or v2.read_bytes() != rebuilt.read_bytes():
        raise RuntimeError("Expected distinct versions and byte-identical candidate builds")
    print("Source commit", commit, "candidate SHA-256", hashlib.sha256(rebuilt.read_bytes()).hexdigest(), flush=True)
    if not args.build_only:
        native = dict(os.environ)
        native.setdefault("CARGO_PROFILE_DEV_DEBUG", "0")
        local_ssl = OUT / "openssl/Library"
        if os.name == "nt" and (local_ssl / "lib/libcrypto.lib").is_file():
            native.setdefault("OPENSSL_DIR", str(local_ssl))
            native.setdefault("OPENSSL_NO_VENDOR", "1")
            native.setdefault("OPENSSL_STATIC", "0")
            native["PATH"] = str(Path(native["OPENSSL_DIR"]) / "bin") + os.pathsep + native["PATH"]
        runner = ["cargo", "run", "--offline", "--locked", "-p", "svm-canary-runner", "--"]
        output = OUT / "evidence"
        demo = [*runner, "demo", str(gate), str(v1), str(v2), str(rebuilt), commit, str(output)]
        if args.load_test:
            regression = build(OUT / "builder/Cargo.toml", "canary_counter", OUT / "regression",
                               toolchain, "candidate,regression-fixture")
            demo.append(str(regression))
        run(demo, env=native)
        report = json.loads((output / "report.json").read_bytes())
        archive = output / "archive.json"
        if hashlib.sha256(archive.read_bytes()).hexdigest() != report["archive_sha256"]:
            raise RuntimeError("Saved archive differs from the verification report")
        if args.load_test:
            if report["replayed_transactions_per_verifier"] != 1000 or report["v0_alt_cpi_transactions"] != 100:
                raise RuntimeError("Unexpected benchmark coverage")
            binary = ROOT / "target/debug" / ("canary-gate.exe" if os.name == "nt" else "canary-gate")
            command = [str(binary), "verify", str(archive), str(v1), str(rebuilt)]
            samples = []
            for _ in range(5):
                started = perf_counter()
                measured = run(command, env=native, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                check = json.loads(measured.stdout)
                if (bytes(check["diff_root"]).hex() != report["diff_root"]
                        or bytes(check["elf_sha256"]).hex() != report["candidate_sha256"]
                        or check["replayed_tx_count"] != report["replayed_transactions_per_verifier"]
                        or check["max_cu_delta_bps"] != report["max_cu_delta_bps"]):
                    raise RuntimeError("Benchmark replay produced different evidence")
                samples.append((perf_counter() - started) * 1000)
            metrics = {
                "environment": "synthetic local LiteSVM",
                "transactions_per_replay": report["replayed_transactions_per_verifier"],
                "v0_alt_cpi_transactions": report["v0_alt_cpi_transactions"],
                "candidate_build_ms": candidate_ms,
                "candidate_rebuild_ms": rebuild_ms,
                "archive_bytes": (output / "archive.json").stat().st_size,
                "standalone_replay_wall_ms": samples,
                "late_regression": report["late_regression"],
                "upgrade_cu": report["execution_cu"],
                "vote_bytes_and_cu": report["attestations_bytes_and_cu"],
            }
            if Path("/usr/bin/time").is_file():
                usage = run(["/usr/bin/time", "-f", "%U %S %M", *command], env=native,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                user_s, system_s, rss_kib = usage.stderr.strip().split()
                metrics["standalone_replay_cpu_seconds"] = float(user_s) + float(system_s)
                metrics["standalone_replay_peak_rss_kib"] = int(rss_kib)
            (output / "load-test.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a", encoding="utf-8") as file:
                    print("## Canary load test - LiteSVM\n", file=file)
                    print(f"- **{metrics['transactions_per_replay']}** transactions per replay, including **{metrics['v0_alt_cpi_transactions']}** with v0, ALT and CPI.", file=file)
                    print(f"- Five replay samples: {', '.join(f'{ms:.1f} ms' for ms in samples)}.", file=file)
                    print(f"- Faulty candidate rejected at transaction #901 in {report['late_regression']['rejection_ms']:.1f} ms.", file=file)
                    print(f"- Peak replay memory: {metrics.get('standalone_replay_peak_rss_kib', 'unavailable')} KiB.", file=file)
                    print("- Full metrics and signed archive are in the canary-mvp artifact.", file=file)
        print("Saved archive verified:", output / "report.json")


if __name__ == "__main__":
    main()
