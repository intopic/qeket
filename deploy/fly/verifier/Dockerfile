# Independent Canary verifier: listener, offline candidate builder and replay runner.
# Build context: a staged copy of the repository root (see deploy-verifiers.yml).

FROM rust:1.95-bookworm AS runner
# The runner derives the gate config PDA from the compiled program ID.
ARG GATE_PROGRAM_ID
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates crates
COPY programs programs
RUN test -n "$GATE_PROGRAM_ID" \
 && sed -i "s/declare_id!(\"[1-9A-HJ-NP-Za-km-z]*\")/declare_id!(\"$GATE_PROGRAM_ID\")/" programs/canary_upgrade_gate/src/lib.rs \
 && grep -q "declare_id!(\"$GATE_PROGRAM_ID\")" programs/canary_upgrade_gate/src/lib.rs
ENV OPENSSL_NO_VENDOR=1 CARGO_PROFILE_RELEASE_LTO=off CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
RUN cargo build --release --locked -p svm-canary-runner

FROM python:3.12-slim-bookworm
# dockerd runs the same offline builder container as CI and the proposer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io git ca-certificates curl bzip2 libssl3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=runner /src/target/release/canary-gate /usr/local/bin/canary-gate
WORKDIR /worker
COPY scripts/requirements-worker.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements-worker.txt
COPY scripts/devnet.py scripts/live_capture.py scripts/live_prestate.py scripts/fly_listener.py scripts/customer_admin.py scripts/worker_http.py scripts/worker_notify.py scripts/solana_tx.py scripts/worker_build.py scripts/worker_vote.py ./
COPY deploy/fly/verifier/start.sh /worker/start.sh
ENTRYPOINT ["sh", "/worker/start.sh"]
