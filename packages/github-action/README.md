# Canary upgrade gate action

Runs the `canary` CLI from `packages/squads-adapter` in a client's repository.

| Command | What it does |
| --- | --- |
| `propose` | Builds the program with the verifiers' builder (offline Docker, pinned platform tools v1.58). Writes it to a buffer and hands the buffer authority to the gate config. Sizes the replay window from recent traffic, then opens a Squads proposal for `propose_buffer_upgrade`. |
| `status` | Reports the latest proposal: votes, window, expiry, and whether it can execute. |
| `execute` | Calls `execute_gated_upgrade` once quorum and the timelock allow. Before that it reports why it waited and exits successfully. |

For `propose` and `execute`, optional `priority-fee` sets the compute-unit price in micro-lamports. It defaults to `0`.

The multisig members still approve the Squads proposal themselves. The Canary verifiers then see the gate's `ProposalCreated` event, rebuild, replay and vote.

## Example

```yaml
name: Gated upgrade
on:
  push:
    branches: [main]
    paths: ["programs/**", "Cargo.lock"]
  schedule:
    - cron: "*/15 * * * *"
jobs:
  propose:
    if: github.event_name == 'push'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: intopic/qeket/packages/github-action@main
        with:
          command: propose
          rpc-url: ${{ secrets.RPC_URL }}
          gate-program: <gate program>
          target-program: <your program>
          multisig: <Squads multisig>
          keypair: ${{ secrets.CANARY_PROPOSER }}
          build-subdir: programs/my_program
          library-name: my_program
          max-replay-txs: "500"
  execute:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-24.04
    steps:
      - uses: intopic/qeket/packages/github-action@main
        with:
          command: execute
          rpc-url: ${{ secrets.RPC_URL }}
          gate-program: <gate program>
          target-program: <your program>
          keypair: ${{ secrets.CANARY_PAYER }}
```

## Requirements

- **The proposer key** (`CANARY_PROPOSER`) is a Squads member with only the Initiate permission. It pays the buffer rent, which the upgrade returns to the spill account. A leaked key can open proposals but cannot approve them.
- **The build must match the verifiers' build.** `build-subdir`, `library-name` and `build-features` must equal the values registered with Canary. Run `propose` before any step that writes into the repository, and only on commits the registered repository URL serves: a push to a branch or a tag, not a pull request merge ref.
- **The replay window must fit the plan.** `max-replay-txs` is the plan's cap. `propose` refuses when the gate's minimum window cannot fit under it at the program's current traffic.
- **Access.** Clients can only use the action from a repository they can read, so it needs a public distribution before the main repository becomes private.
