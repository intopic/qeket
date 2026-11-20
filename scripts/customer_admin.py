"""Manage the customer configuration loaded by a Canary worker."""
import argparse
import json
from pathlib import Path

from fly_listener import PLANS, customer_policies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("action", choices=["set", "suspend", "plan"])
    parser.add_argument("customer")
    parser.add_argument("--plan", choices=PLANS)
    parser.add_argument("--target", action="append")
    parser.add_argument("--cooldown-minutes", type=int, default=0)
    parser.add_argument("--active-until")
    parser.add_argument("--repo-url")
    parser.add_argument("--library-name")
    parser.add_argument("--build-subdir")
    parser.add_argument("--webhook-url")
    parser.add_argument("--live-prestate", action="store_true")
    parser.add_argument("--seed-account", action="append")
    args = parser.parse_args()
    customers = json.loads(args.file.read_text(encoding="utf-8")) if args.file.exists() else {}
    if args.action == "set":
        if not args.target or not args.plan:
            parser.error("set requires --target and --plan")
        entry = dict(targets=args.target, plan=args.plan, active=True, cooldown_minutes=args.cooldown_minutes)
        if args.active_until:
            entry["active_until"] = args.active_until
        previous = customers.get(args.customer, {})
        for field in ("repo_url", "library_name", "build_subdir", "webhook_url"):
            value = getattr(args, field)
            if value is not None:
                entry[field] = value
            elif field in previous:
                entry[field] = previous[field]
        if args.seed_account or args.live_prestate:
            entry["seed_accounts"] = args.seed_account or []
        elif "seed_accounts" in previous:
            entry["seed_accounts"] = previous["seed_accounts"]
        customers[args.customer] = entry
    elif args.customer not in customers:
        parser.error("unknown customer")
    elif args.action == "suspend":
        customers[args.customer]["active"] = False
    else:
        if not args.plan:
            parser.error("plan requires --plan")
        entry = customers[args.customer]
        entry["plan"] = args.plan
        for field in ("max_upgrades_per_month", "max_replay_txs", "monthly_proposals"):
            entry.pop(field, None)
    payload = json.dumps(customers, indent=2)
    customer_policies(payload)
    staged = args.file.with_suffix(args.file.suffix + ".tmp")
    staged.write_text(payload + "\n", encoding="utf-8")
    staged.replace(args.file)


if __name__ == "__main__":
    main()
