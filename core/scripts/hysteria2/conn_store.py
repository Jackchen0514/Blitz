#!/usr/bin/env python3
"""CLI helper for limit.sh: tracks active IPs per user in a JSON file.

Replaces the MongoDB 'active_connections' collection used on main with a
flock-protected JSON file, since the nodb variant has no database server.
"""

import init_paths
import fcntl
import json
import sys
from pathlib import Path

from paths import CONNECTIONS_FILE
from db.database import db

LOCK_FILE = Path(str(CONNECTIONS_FILE) + '.lock')


def _with_lock(fn):
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, 'w') as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _load() -> dict:
    try:
        return json.loads(CONNECTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    CONNECTIONS_FILE.write_text(json.dumps(data))


def add_ip(username: str, ip: str):
    def _do():
        data = _load()
        ips = set(data.get(username, []))
        ips.add(ip)
        data[username] = sorted(ips)
        _save(data)
    _with_lock(_do)


def remove_ip(username: str, ip: str):
    def _do():
        data = _load()
        ips = set(data.get(username, []))
        ips.discard(ip)
        if ips:
            data[username] = sorted(ips)
        else:
            data.pop(username, None)
        _save(data)
    _with_lock(_do)


def get_ips(username: str):
    data = _with_lock(_load)
    print(json.dumps(data.get(username, [])))


def get_count(username: str):
    data = _with_lock(_load)
    print(len(data.get(username, [])))


def clean():
    _with_lock(lambda: _save({}))


def is_unlimited(username: str):
    user = db.get_user(username) if db else None
    print('true' if user and user.get('unlimited_user') else 'false')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} {{add_ip|remove_ip|get_ips|get_count|clean|is_unlimited}} ...", file=sys.stderr)
        sys.exit(1)

    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'add_ip':
        add_ip(*args)
    elif cmd == 'remove_ip':
        remove_ip(*args)
    elif cmd == 'get_ips':
        get_ips(*args)
    elif cmd == 'get_count':
        get_count(*args)
    elif cmd == 'clean':
        clean()
    elif cmd == 'is_unlimited':
        is_unlimited(*args)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
