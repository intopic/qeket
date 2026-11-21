"""Rebuild a candidate from its commit with the same offline container the proposer uses."""
import argparse
import json
import sys
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

IMAGE = "canary-live-builder"
# The SBF compiler is mounted read-only from pinned platform tools. The image
# provides only the host linker and C runtime for build scripts.
DOCKERFILE = """FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libssl-dev libclang-dev && rm -rf /var/lib/apt/lists/*
"""
SKIPPED = {".git", ".cargo", "target", "rust-toolchain", "rust-toolchain.toml"}
FEATURE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(/[A-Za-z0-9_][A-Za-z0-9_-]*)?")


class BuildFailed(RuntimeError):
    pass


def run(args, cwd=None, timeout=1800, stdout=None, env=None):
    try:
        result = subprocess.run(args, cwd=cwd, timeout=timeout, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildFailed(f"{Path(args[0]).name} could not complete: {type(exc).__name__}") from None
    if result.returncode:
        tail = result.stderr.decode(errors="replace")[-1500:]
        raise BuildFailed(f"{Path(args[0]).name} exited with {result.returncode}: {tail}")
    return result


def platform_root(toolchain):
    """Return the platform-tools directory that holds rust/ and llvm/."""
    if os.environ.get("CANARY_PLATFORM_TOOLS"):
        # Hosts without rustup (the Fly verifier image) unpack the pinned
        # release directly.
        return Path(os.environ["CANARY_PLATFORM_TOOLS"]).resolve()
    compiler = run(["rustup", "which", "--toolchain", toolchain, "rustc"]).stdout.decode().strip()
    return Path(os.path.realpath(compiler)).parent.parent.parent


def copy_source(source, destination):
    """Copy the build tree. Links and local Cargo overrides never reach the build."""
    for root, directories, files in os.walk(source):
        relative = Path(root).relative_to(source)
        # Cargo reads overrides from the build root only. Nested names are source.
        skipped = SKIPPED if relative == Path(".") else {".git"}
        directories[:] = sorted(d for d in directories if d not in skipped)
        (destination / relative).mkdir(parents=True, exist_ok=True)
        for name in [*directories, *files]:
            if (Path(root) / name).is_symlink():
                raise BuildFailed(f"symbolic link in build source: {relative / name}")
        for name in files:
            if name not in skipped:
                shutil.copyfile(Path(root) / name, destination / relative / name)
    if not (destination / "Cargo.toml").is_file() or not (destination / "Cargo.lock").is_file():
        raise BuildFailed("build directory needs Cargo.toml and a committed Cargo.lock")


def vendor(directory):
    (directory / ".cargo").mkdir()
    with (directory / ".cargo/config.toml").open("wb") as config:
        run(["cargo", "vendor", "--locked", "vendor"], cwd=directory, stdout=config)


def ensure_image():
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        # Host networking lets daemons without a bridge network run apt-get.
        subprocess.run(["docker", "build", "--network", "host", "-t", IMAGE, "-"], input=DOCKERFILE.encode(), check=True,
                       capture_output=True, timeout=1800)


def container_build(source, out, features, platform):
    """Compile without network, privileges or write access to the source."""
    out.mkdir(parents=True, exist_ok=True)
    # Hosts with threaded cgroups (Fly Sprites) cannot apply resource limits.
    # The limits bound the build only and do not change its output.
    limits = (["--cpus", "2", "--memory", "5g", "--memory-swap", "5g"]
              if os.environ.get("CANARY_DOCKER_RESOURCE_LIMITS", "1") != "0" else [])
    command = ["cargo", "build", "--release", "--locked", "--offline", "--target", "sbpf-solana-solana",
               "--target-dir", "/out"]
    if features:
        command += ["--features", ",".join(features)]
    if os.environ.get("CANARY_DOCKER_ROOT") == "1":
        # Some hosts start containers only as uid 0. Without capabilities that
        # user can write only to a world-writable output and must hand it back.
        user = []
        out.chmod(0o777)
        command = ["sh", "-c", '"$@"; status=$?; chmod -R a+rwX /out; exit $status', "sh", *command]
    else:
        user = ["--user", f"{os.getuid()}:{os.getgid()}"]
    args = ["docker", "run", "--rm", "--network", "none", *limits,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *user,
            "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g", "-v", f"{source}:/src:ro",
            "-v", f"{platform}:/platform:ro", "-v", f"{out}:/out", "-w", "/src",
            "-e", "HOME=/tmp", "-e", "CARGO_NET_OFFLINE=true", "-e", "PATH=/platform/rust/bin:/usr/bin:/bin",
            IMAGE, *command]
    run(args, timeout=3600)


def check_options(subdir, library, features):
    path = PurePosixPath(subdir or ".")
    if path.is_absolute() or ".." in path.parts or "\\" in str(path):
        raise BuildFailed("build_subdir must stay inside the repository")
    if not library or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", library):
        raise BuildFailed("library_name is required to locate the build output")
    if any(not FEATURE.fullmatch(feature) for feature in features):
        raise BuildFailed("invalid cargo feature")
    return path


def fetch_commit(repo_url, commit, destination):
    """Fetch one commit over anonymous HTTPS and check the checked-out object name."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not repo_url.startswith("https://"):
        raise BuildFailed("expected an HTTPS repository and a full commit hash")
    home = destination.parent / "git-home"
    home.mkdir(exist_ok=True)
    env = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"), HOME=str(home), GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    git = ["git", "-c", "credential.helper=", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
           "-c", "core.symlinks=false", "-c", f"core.hooksPath={os.devnull}", "-C", str(destination)]
    destination.mkdir()
    run(git + ["init", "--quiet"], env=env)
    run(git + ["fetch", "--quiet", "--depth", "1", "--no-tags", repo_url, commit], timeout=900, env=env)
    run(git + ["checkout", "--quiet", "--detach", "FETCH_HEAD"], env=env)
    head = run(git + ["rev-parse", "HEAD"], env=env).stdout.decode().strip()
    if head != commit:
        raise BuildFailed("fetched commit differs from the proposal")


def build_tree(source, out, variants, toolchain):
    """Vendor once, then compile each (name, features) variant into out/name."""
    with tempfile.TemporaryDirectory(prefix="canary-build-") as scratch:
        tree = Path(scratch) / "src"
        copy_source(source, tree)
        vendor(tree)
        platform = platform_root(toolchain)
        ensure_image()
        for name, features in variants:
            container_build(tree, out / name, features, platform)


def build_candidate(repo_url, commit, subdir, library, features, work, toolchain):
    path = check_options(subdir, library, features)
    work.mkdir(parents=True, exist_ok=True)
    checkout = work / "checkout"
    if checkout.exists():
        shutil.rmtree(checkout)
    fetch_commit(repo_url, commit, checkout)
    source = checkout.joinpath(*path.parts)
    if not source.resolve().is_relative_to(checkout.resolve()) or not source.is_dir():
        raise BuildFailed("build_subdir is not a directory in the commit")
    build_tree(source, work, [("candidate", list(features))], toolchain)
    output = work / "candidate/sbpf-solana-solana/release" / f"{library.replace('-', '_')}.so"
    if output.resolve() != output.absolute() or not output.is_file():
        raise BuildFailed("build did not produce the configured library")
    return output


def remote_build(host, policy, commit, work):
    """Fetch an ELF from this verifier's independently provisioned build host."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host):
        raise BuildFailed("invalid builder hostname")
    request = dict(repo_url=policy.repo_url, commit=commit, subdir=policy.build_subdir or ".",
                   library=policy.library_name, features=list(policy.build_features))
    work.mkdir(parents=True, exist_ok=True)
    output = work / "candidate.so"
    ssh = ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/run/canary/known_hosts",
           "-o", "ConnectTimeout=15", "-i", "/run/canary/build_key", "-p", "2222", "builder@" + host]
    try:
        with output.open("wb") as elf:
            command = ["sh", "-c", 'ulimit -f 20480 && exec "$@"', "sh", *ssh]
            result = subprocess.run(command, input=json.dumps(request).encode(), stdout=elf,
                                    stderr=subprocess.DEVNULL, timeout=5400)
        if result.returncode or not 0 < output.stat().st_size <= 10 * 1024 * 1024:
            raise BuildFailed("remote builder did not return a program")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildFailed(f"remote build unavailable: {type(exc).__name__}") from None
    return output


def serve_build():
    """Restricted SSH command on a dedicated builder with no verifier credentials."""
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise BuildFailed("build request too large")
    args = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix="canary-job-") as directory:
        output = build_candidate(args["repo_url"], args["commit"], args["subdir"], args["library"],
                                 args["features"], Path(directory), None)
        if not 0 < output.stat().st_size <= 10 * 1024 * 1024:
            raise BuildFailed("invalid build output size")
        with output.open("rb") as elf:
            shutil.copyfileobj(elf, sys.stdout.buffer)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve-build"]:
        serve_build()
        sys.exit(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("variants", nargs="+", metavar="NAME=FEATURES")
    args = parser.parse_args()
    variants = []
    for variant in args.variants:
        name, _, features = variant.partition("=")
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            parser.error("invalid variant name")
        variants.append((name, [f for f in features.split(",") if f]))
    check_options(".", "unused", [f for _, fs in variants for f in fs])
    # CANARY_PLATFORM_TOOLS, used by the GitHub Action, takes precedence over a rustup toolchain.
    build_tree(args.source.resolve(), args.out.resolve(), variants, os.environ.get("CANARY_SBF_TOOLCHAIN"))
