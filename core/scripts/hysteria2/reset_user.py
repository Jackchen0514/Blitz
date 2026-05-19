#!/usr/bin/env python3

import init_paths
import sys
from db.database import db

def reset_user(username):
    """
    Resets the data usage, status, and creation date of a user in the database.

    Args:
        username (str): The username to reset.

    Returns:
        int: 0 on success, 1 on failure.
    """
    if db is None:
        print("Error: Database connection failed.")
        return 1

    try:
        user = db.get_user(username)
        if not user:
            print(f"Error: User '{username}' not found in the database.")
            return 1

        db.update_user(username, {'status': 'On-hold', 'blocked': False})
        db.unset_user_fields(username, ['account_creation_date', 'download_bytes', 'upload_bytes'])
        print(f"User '{username}' has been reset successfully.")
        return 0

    except Exception as e:
        print(f"An error occurred while resetting the user: {e}")
        return 1

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <username>")
        sys.exit(1)

    username_to_reset = sys.argv[1].lower()
    exit_code = reset_user(username_to_reset)
    sys.exit(exit_code)