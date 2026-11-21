# Independent Fly verifiers

Each verifier uses its own signing Machine and a dedicated build Machine in the same Fly private network. The build host fetches and compiles the customer's commit. The signing Machine receives the resulting ELF over SSH, compares it with the proposal, replays the archive and signs the evidence. Budget for four Machines and their volumes when deploying both pairs.

For each suffix `a` and `b`, provision the apps named in the matching TOML files. Generate distinct Ed25519 SSH client and host keys for each pair. Install the following as Fly secrets using the original multiline OpenSSH format for private keys.

| App | Secrets |
| --- | --- |
| `canary-builder-<suffix>` | `BUILDER_HOST_KEY` is the SSH host private key. `BUILDER_CLIENT_PUBKEY` is the matching verifier's SSH client public key. |
| `canary-verifier-<suffix>` | `CANARY_BUILDER_SSH_KEY` is its SSH client private key. `CANARY_BUILDER_KNOWN_HOSTS` is a single line of the form `[canary-builder-<suffix>.internal]:2222 ssh-ed25519 <host-public-key-base64>`. Install the verifier's own `CANARY_VERIFIER_KEYPAIR`, `RPC_URL` and `CANARY_CUSTOMERS` here. |

The builder accepts only the fixed build command over the private network. Its host key must match the pinned key on the verifier. Each signing Machine has its own Solana key and runs without Docker socket access.

Install four app-scoped deploy tokens in GitHub Actions as `FLY_API_TOKEN_BUILDER_A`, `FLY_API_TOKEN_BUILDER_B`, `FLY_API_TOKEN_VERIFIER_A` and `FLY_API_TOKEN_VERIFIER_B`. Run **Deploy Fly verifiers** to deploy each builder before its verifier. The health endpoint reports worker checkpoint progress and pending cleanup. A successful deployment still needs a finalized Devnet vote from each verifier to demonstrate that its build host, RPC and signing key work together.
