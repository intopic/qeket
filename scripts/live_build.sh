#!/usr/bin/env bash
set -euo pipefail
root=$(pwd)
tmp=$(mktemp -d)
cp -r examples/counter/{Cargo.toml,Cargo.lock,src} "$tmp/"
mkdir "$tmp/.cargo"
(cd "$tmp" && cargo vendor --locked vendor > .cargo/config.toml)
compiler=$(rustup which --toolchain "$CANARY_SBF_TOOLCHAIN" rustc)
sdk=$(dirname "$(dirname "$compiler")")
# Install host linker and C runtime before isolating the build. Neither the source
# tree nor the SDK is in the Docker build context.
docker build -t canary-live-builder - <<'DOCKER'
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libssl-dev libclang-dev && rm -rf /var/lib/apt/lists/*
DOCKER
image=$(docker image inspect --format '{{.Id}}' canary-live-builder)
for variant in baseline candidate rebuild; do
  out="$root/target/mvp/$variant"
  mkdir -p "$out"
  features=()
  if [[ $variant != baseline ]]; then features=(--features candidate); fi
  docker run --rm --network none --cpus 2 --memory 5g --memory-swap 5g --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,size=1g \
    -v "$tmp:/src:ro" -v "$(dirname "$sdk"):/platform:ro" -v "$out:/out" -w /src \
    -e HOME=/tmp -e CARGO_NET_OFFLINE=true -e PATH=/platform/rust/bin:/usr/bin:/bin \
    "$image" cargo build --release --locked --offline --target sbpf-solana-solana --target-dir /out "${features[@]}"
done
a=target/mvp/candidate/sbpf-solana-solana/release/canary_counter.so
b=target/mvp/rebuild/sbpf-solana-solana/release/canary_counter.so
cmp "$a" "$b"
solana-verify get-executable-hash "$a"
