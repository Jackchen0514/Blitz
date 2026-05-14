#!/usr/bin/env python3

import init_paths
import sys
import argparse
from datetime import datetime, date
from db.database import db


def _is_reset_due(today: date, creation_date_str: str, last_reset_str: str | None) -> bool:
    """
    Returns True if traffic reset is due for a user today.

    Reset is due when:
    - today's day-of-month matches the creation day-of-month (or last day of month
      if creation day exceeds current month length)
    - AND the last reset was not already done in this current monthly cycle
      (i.e., last_reset < this month's reset date)
    """
    try:
        creation_dt = datetime.strptime(creation_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False

    creation_day = creation_dt.day

    # Determine the reset date for the current month
    # (clamp to last day of month if creation_day exceeds month length)
    import calendar
    last_day_of_month = calendar.monthrange(today.year, today.month)[1]
    reset_day = min(creation_day, last_day_of_month)
    this_month_reset = date(today.year, today.month, reset_day)

    if today != this_month_reset:
        return False

    if last_reset_str:
        try:
            last_reset = datetime.strptime(last_reset_str, "%Y-%m-%d").date()
            if last_reset >= this_month_reset:
                return False
        except (ValueError, TypeError):
            pass

    return True


def reset_traffic_all() -> int:
    """Reset traffic for all users whose monthly reset date falls on today."""
    if db is None:
        print("Error: Database connection failed.", file=sys.stderr)
        return 1

    today = date.today()
    users = db.list_users()
    if not users:
        print("No users found.")
        return 0

    reset_count = 0
    for user in users:
        username = user.get('_id')
        creation_date = user.get('account_creation_date')
        expiration_days = user.get('expiration_days', 0)
        last_reset = user.get('last_traffic_reset')

        if not creation_date:
            continue

        # Skip expired users
        if expiration_days > 0:
            from datetime import timedelta
            creation_dt = datetime.strptime(creation_date, "%Y-%m-%d").date()
            expiry = creation_dt + timedelta(days=expiration_days)
            if today > expiry:
                continue

        if not _is_reset_due(today, creation_date, last_reset):
            continue

        try:
            db.update_user(username, {
                'upload_bytes': 0,
                'download_bytes': 0,
                'blocked': False,
                'last_traffic_reset': today.strftime("%Y-%m-%d"),
            })
            print(f"Traffic reset for user '{username}'.")
            reset_count += 1
        except Exception as e:
            print(f"Error resetting traffic for '{username}': {e}", file=sys.stderr)

    print(f"Monthly traffic reset complete. {reset_count} user(s) reset.")
    return 0


def reset_traffic_user(username: str) -> int:
    """Manually reset traffic for a specific user."""
    if db is None:
        print("Error: Database connection failed.", file=sys.stderr)
        return 1

    username_lower = username.lower()
    user = db.get_user(username_lower)
    if not user:
        print(f"Error: User '{username}' not found.", file=sys.stderr)
        return 1

    today = date.today()
    try:
        db.update_user(username_lower, {
            'upload_bytes': 0,
            'download_bytes': 0,
            'blocked': False,
            'last_traffic_reset': today.strftime("%Y-%m-%d"),
        })
        print(f"Traffic reset for user '{username}' successfully.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset user traffic usage.")
    parser.add_argument("--all", action="store_true",
                        help="Reset traffic for all users whose monthly reset date is today.")
    parser.add_argument("--username", "-u",
                        help="Reset traffic for a specific user (manual override).")
    args = parser.parse_args()

    if args.username:
        sys.exit(reset_traffic_user(args.username))
    elif args.all:
        sys.exit(reset_traffic_all())
    else:
        parser.print_help()
        sys.exit(1)
