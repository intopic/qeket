# Single Fly verifier

The `canary-verifier-a` app uses one Machine and one vote key. The worker listens for proposals, captures live pre-state, rebuilds the candidate in offline Docker, replays the window and signs one vote on success. Configure the on-chain policy with this address and `threshold: 1`. SQLite and job files use the Machine's 10 GB volume.

## Setup

1. Create the app and set its secrets:

   | Secret | Value |
   | --- | --- |
   | `CANARY_VERIFIER_KEYPAIR` | This verifier's own vote key. Install `scripts/requirements-worker.txt`, then generate it locally with `python scripts/solana_tx.py keygen`, which prints the secret on stdout and the address on stderr. Never derive it from a CI secret. |
   | `CANARY_CUSTOMERS` | The customer policies (see the main README). |
   | `CANARY_VOTE_PRIORITY_FEE` | Optional compute-unit price in micro-lamports. Defaults to zero. |
   | `RPC_URL` | Optional. The deploy workflow copies the repository's `RPC_URL` if the app has none. |
   | `CANARY_ADMIN_TOKEN`, `HELIUS_WEBHOOK_AUTHORIZATION`, `CANARY_SLACK_WEBHOOK`, `CANARY_TELEGRAM_BOT_TOKEN`, `CANARY_TELEGRAM_CHAT_ID` | Optional status endpoint, webhook ingress and operator notices. |

2. Create an app-scoped deploy token and store it in GitHub Actions as `FLY_API_TOKEN_VERIFIER_A`.

3. Set the GitHub Actions variable `CANARY_DEVNET_GATE_PROGRAM_ID` to the program ID from a successful public Devnet 1/1 run. The workflow uses the same address for the image build and listener, and checks that the address resolves to a finalized Devnet executable. The Live Devnet workflow publishes `gate-program-id.txt` in its cycle artifact. The test fixture derives a fresh address per run unless a gate keypair is supplied.

4. Run **Deploy Fly verifier**. It refuses to deploy an app that lacks `CANARY_VERIFIER_KEYPAIR` or `CANARY_CUSTOMERS`.

The first start downloads platform-tools v1.58 to the volume and checks its SHA-256. `CANARY_CLUSTER` in the TOML file selects `devnet` or `mainnet-beta`. The worker refuses an `RPC_URL` from another cluster.

Customer `webhook_url` values receive JSON lifecycle notices over HTTPS. The worker checks DNS results and connects to a validated public IP, keeping TLS verification for the original hostname. Redirects are refused. Delivery failures do not affect proposal decisions.

## Isolation

Builds run in Docker with no network, no capabilities and a read-only source tree. The listener needs the Docker socket to start them, which amounts to root on the Machine, where its vote key also lives. A container escape would expose that key. The same verifier key can serve multiple customers.

Before Mainnet, move builds to a Machine without a vote key. Set `CANARY_BUILDER_HOST` to that host. The worker then sends the build request over SSH, with a pinned host key, to the restricted command `worker_build.py serve-build`. The Fly configuration for such a build host is in commit `9238584`.

A successful deployment still needs a finalized Devnet vote from the worker to show that the build, RPC and signing key work together.

Scaling capacity means assigning different proposals to additional workers. The current deployment uses a local SQLite ledger. A multi-Machine deployment needs shared admission and job claiming before distributing that queue.
