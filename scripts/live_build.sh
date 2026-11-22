#!/usr/bin/env bash
set -euo pipefail
# The proposer and every verifier use the same offline container build.
python3 scripts/worker_build.py examples/counter target/mvp baseline= candidate=candidate
a=target/mvp/candidate/sbpf-solana-solana/release/canary_counter.so
sha256sum "$a"
