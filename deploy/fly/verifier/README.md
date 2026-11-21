# Independent Fly verifiers

Two apps, one Machine each: `canary-verifier-a` in `fra` and `canary-verifier-b` in `iad`. Each Machine listens for gate proposals, captures the live pre-state, rebuilds the candidate in an offline Docker container, replays the window with `canary-gate` and signs its vote. Each has its own Solana key and its own SQLite ledger on a 10 GB volume.

## Setup for each app

1. Create the app and set its secrets:

   | Secret | Value |
   | --- | --- |
   | `CANARY_VERIFIER_KEYPAIR` | This verifier's own vote key. Generate it locally with `python scripts/solana_tx.py keygen`, which prints the secret on stdout and the address on stderr. Never derive it from a CI secret. |
   | `CANARY_CUSTOMERS` | The customer policies (see the main README). |
   | `RPC_URL` | Optional. The deploy workflow copies the repository's `RPC_URL` if the app has none. |
   | `CANARY_ADMIN_TOKEN`, `HELIUS_WEBHOOK_AUTHORIZATION`, `CANARY_SLACK_WEBHOOK`, `CANARY_TELEGRAM_BOT_TOKEN`, `CANARY_TELEGRAM_CHAT_ID` | Optional status endpoint, webhook ingress and operator notices. |

2. Create an app-scoped deploy token and store it in GitHub Actions as `FLY_API_TOKEN_VERIFIER_A` or `FLY_API_TOKEN_VERIFIER_B`.

3. Run **Deploy Fly verifiers**. It refuses to deploy an app that lacks `CANARY_VERIFIER_KEYPAIR` or `CANARY_CUSTOMERS`.

The first start downloads platform-tools v1.58 to the volume and checks its SHA-256. `CANARY_CLUSTER` in the TOML files selects `devnet` or `mainnet-beta`. The worker refuses an `RPC_URL` from another cluster.

## Isolation

Builds run in Docker with no network, no capabilities and a read-only source tree. The listener needs the Docker socket to start them, which amounts to root on the Machine, where its vote key also lives. A container escape would expose that key. Both verifiers build the same proposer-chosen commit, and their keys are shared by all customers. This is accepted for Devnet.

Before Mainnet, move builds to a Machine without a vote key. Set `CANARY_BUILDER_HOST` to that host. The worker then sends the build request over SSH, with a pinned host key, to the restricted command `worker_build.py serve-build`. The Fly configuration for such a build host is in commit `9238584`.

A successful deployment still needs a finalized Devnet vote from each verifier to show that its build, RPC and signing key work together.
