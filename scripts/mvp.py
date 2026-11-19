"""Build the trusted local example twice, then exercise the complete gate in LiteSVM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

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


def build(manifest, crate, directory, toolchain, candidate=False):
    env = dict(os.environ)
    env["CARGO_NET_OFFLINE"] = "true"
    if toolchain:
        args = ["cargo", "+" + toolchain, "build", "--manifest-path", str(manifest), "--target",
                "sbpf-solana-solana", "--target-dir", str(directory), "--release", "--locked"]
        env["CARGO_TARGET_SBPF_SOLANA_SOLANA_RUSTFLAGS"] = f"--remap-path-prefix={manifest.parent}=fixture"
        if candidate:
            args += ["--features", "candidate"]
        run(args, env=env)
        return directory / "sbpf-solana-solana/release" / (crate + ".so")
    args = ["cargo", "build-sbf", "--tools-version", "v1.58", "--manifest-path", str(manifest), "--sbf-out-dir", str(directory / "deploy")]
    if candidate:
        args += ["--features", "candidate"]
    args += ["--", "--locked"]
    run(args, env=dict(env, CARGO_TARGET_DIR=str(directory)))
    return directory / "deploy" / (crate + ".so")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
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
    v2 = build(OUT / "builder/Cargo.toml", "canary_counter", OUT / "candidate", toolchain, True)
    rebuilt = build(OUT / "verifier/Cargo.toml", "canary_counter", OUT / "rebuild", toolchain, True)
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
        run([*runner, "demo", str(gate), str(v1), str(v2), str(rebuilt), commit, str(output)], env=native)
        # Exercise the standalone reader against the evidence actually persisted to disk.
        saved = run([*runner, "verify", str(output / "archive.json"), str(v1), str(rebuilt)],
                    env=native, stdout=subprocess.PIPE, text=True)
        check = json.loads(saved.stdout)
        report = json.loads((output / "report.json").read_bytes())
        if (bytes(check["diff_root"]).hex() != report["diff_root"]
                or bytes(check["elf_sha256"]).hex() != report["candidate_sha256"]
                or check["replayed_tx_count"] != report["replayed_transactions_per_verifier"]
                or hashlib.sha256((output / "archive.json").read_bytes()).hexdigest() != report["archive_sha256"]):
            raise RuntimeError("Saved evidence failed independent replay")
        print("Saved archive verified:", output / "report.json")


if __name__ == "__main__":
    main()
