#!/usr/bin/env bash
# Pinned host Rust and SBF platform tools for the live Devnet jobs.
set -euo pipefail
rustup toolchain install 1.95.0 --profile minimal
rustup override set 1.95.0
curl -fsSL --retry 3 --retry-all-errors https://github.com/anza-xyz/platform-tools/releases/download/v1.58/platform-tools-linux-x86_64.tar.bz2 -o "$RUNNER_TEMP/tools.tbz"
echo "7b82eb420b5aebd1e5590db0ae25c523eca06724d4238ae02139690535636c37  $RUNNER_TEMP/tools.tbz" | sha256sum -c -
mkdir "$RUNNER_TEMP/sdk"
tar xf "$RUNNER_TEMP/tools.tbz" -C "$RUNNER_TEMP/sdk"
compiler=$(find "$RUNNER_TEMP/sdk" -path '*/rust/bin/rustc' -print -quit)
rustup toolchain link "$CANARY_SBF_TOOLCHAIN" "$(dirname "$(dirname "$compiler")")"
