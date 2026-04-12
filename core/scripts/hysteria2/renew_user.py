#!/usr/bin/env python3

import init_paths
import sys
import argparse
from datetime import datetime, timedelta
from db.database import db


def renew_user(username: str, extend_days: int) -> int:
    if db is None:
        print("Error: Database connection failed.", file=sys.stderr)
        return 1

    username_lower = username.lower()
    user = db.get_user(username_lower)
    if not user:
        print(f"Error: User '{username}' not found.", file=sys.stderr)
        return 1

    creation_date = user.get('account_creation_date')
    expiration_days = user.get('expiration_days', 0)

    if not creation_date:
        print(f"Error: User '{username}' has no active creation date (On-hold).", file=sys.stderr)
        return 1

    creation_dt = datetime.strptime(creation_date, "%Y-%m-%d")
    current_expiry = creation_dt + timedelta(days=expiration_days)
    new_expiry = current_expiry + timedelta(days=extend_days)
    new_expiration_days = (new_expiry - creation_dt).days

    try:
        db.update_user(username_lower, {
            'expiration_days': new_expiration_days,
            'blocked': False,
        })
        print(
            f"User '{username}' renewed successfully. "
            f"New expiry: {new_expiry.strftime('%Y-%m-%d')} "
            f"(+{extend_days} days from {current_expiry.strftime('%Y-%m-%d')})."
        )
        return 0
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Renew a user's subscription by extending expiry date.")
    parser.add_argument("username", help="The username to renew.")
    parser.add_argument("--extend-days", "-d", type=int, required=True,
                        help="Number of days to extend from current expiry date.")
    args = parser.parse_args()
    sys.exit(renew_user(args.username, args.extend_days))
