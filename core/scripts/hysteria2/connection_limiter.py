#!/usr/bin/env python3
"""
Per-user concurrent connection limiter for Hysteria2.
Monitors server logs and kicks users who exceed MAX_CONNECTIONS.
"""

import init_paths
import json
import logging
import os
import re
import signal
import subprocess
import sys
from collections import defaultdict
from threading import Lock

from dotenv import dotenv_values
from hysteria2_api import Hysteria2Client

from db.database import db
from paths import CONFIG_ENV, CONFIG_FILE

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s: [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger()

SERVICE_NAME = 'hysteria-conn-limit.service'
API_BASE_URL = 'http://127.0.0.1:25413'
DEFAULT_MAX_CONNECTIONS = 2

# username → active connection count
_counts: dict[str, int] = defaultdict(int)
_lock = Lock()

# Matches both "client connected" and "client disconnected" Hysteria2 log lines.
# Hysteria2 logs in console format: ...TAB client connected TAB {"addr":"...","id":"username",...}
# The event name is NOT quoted in the log line.
_LOG_RE = re.compile(
    r'(?P<event>client connected|client disconnected)'
    r'.*?"id":\s*"(?P<username>[^"]+)"'
)


def _get_secret() -> str | None:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get('trafficStats', {}).get('secret')
    except Exception:
        return None


def _get_max_connections() -> int:
    try:
        val = dotenv_values(CONFIG_ENV).get('MAX_CONNECTIONS', '')
        return int(val) if str(val).isdigit() else DEFAULT_MAX_CONNECTIONS
    except Exception:
        return DEFAULT_MAX_CONNECTIONS


def _is_unlimited(username: str) -> bool:
    if db is None:
        return False
    user = db.get_user(username)
    return bool(user.get('unlimited_user', False)) if user else False


def _kick(username: str, secret: str):
    try:
        Hysteria2Client(base_url=API_BASE_URL, secret=secret).kick_clients([username])
        logger.warning(f'Kicked {username} — exceeded connection limit.')
    except Exception as e:
        logger.error(f'Failed to kick {username}: {e}')


def _handle_line(line: str, secret: str, max_conn: int):
    m = _LOG_RE.search(line)
    if not m:
        return

    event = m.group('event')
    username = m.group('username')

    with _lock:
        if event == 'client connected':
            _counts[username] += 1
            count = _counts[username]
            logger.info(f'{username} connected — active connections: {count}')

            if count > max_conn:
                if _is_unlimited(username):
                    logger.info(f'{username} is unlimited, skipping.')
                    return
                logger.warning(
                    f'{username} has {count} connections (max {max_conn}) — kicking.'
                )
                _kick(username, secret)

        elif event == 'client disconnected':
            if _counts[username] > 0:
                _counts[username] -= 1
            count = _counts[username]
            logger.info(f'{username} disconnected — active connections: {count}')
            if count == 0:
                del _counts[username]


def _run():
    secret = _get_secret()
    if not secret:
        logger.error('Cannot read trafficStats.secret from config.json. Exiting.')
        sys.exit(1)

    max_conn = _get_max_connections()
    logger.info(f'Connection limiter started. MAX_CONNECTIONS={max_conn}')

    def _stop(sig, frame):
        logger.info('Connection limiter stopping.')
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    proc = subprocess.Popen(
        ['journalctl', '-u', 'hysteria-server.service', '-f', '--no-pager', '-o', 'cat'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    for line in proc.stdout:
        _handle_line(line.strip(), secret, max_conn)


def _install_service():
    script_path = os.path.abspath(__file__)
    venv_python = '/etc/hysteria/hysteria2_venv/bin/python'
    unit = f"""[Unit]
Description=Hysteria2 Connection Limiter
After=network.target hysteria-server.service
Requires=hysteria-server.service

[Service]
Type=simple
ExecStart={venv_python} {script_path} run
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""
    with open(f'/etc/systemd/system/{SERVICE_NAME}', 'w') as f:
        f.write(unit)
    os.system('systemctl daemon-reload')
    os.system(f'systemctl enable {SERVICE_NAME}')
    os.system(f'systemctl start {SERVICE_NAME}')
    print(f'Connection limiter service started.')


def _uninstall_service():
    os.system(f'systemctl stop {SERVICE_NAME} 2>/dev/null')
    os.system(f'systemctl disable {SERVICE_NAME} 2>/dev/null')
    unit_file = f'/etc/systemd/system/{SERVICE_NAME}'
    if os.path.exists(unit_file):
        os.remove(unit_file)
    os.system('systemctl daemon-reload')
    print('Connection limiter service stopped and removed.')


def _set_config(max_connections: int | None):
    env_file = str(CONFIG_ENV)
    if not os.path.exists(env_file):
        print(f'Config file not found: {env_file}')
        return

    lines = open(env_file).readlines()
    key = 'MAX_CONNECTIONS'
    updated = False

    if max_connections is not None:
        new_lines = []
        for line in lines:
            if line.startswith(f'{key}='):
                new_lines.append(f'{key}={max_connections}\n')
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f'\n{key}={max_connections}\n')
        with open(env_file, 'w') as f:
            f.writelines(new_lines)
        print(f'MAX_CONNECTIONS set to {max_connections}.')

    # Restart service to pick up new config
    os.system(f'systemctl is-active --quiet {SERVICE_NAME} && systemctl restart {SERVICE_NAME}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''

    if cmd == 'run':
        _run()
    elif cmd == 'start':
        _install_service()
    elif cmd == 'stop':
        _uninstall_service()
    elif cmd == 'config':
        max_conn = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
        _set_config(max_conn)
    else:
        print('Usage: connection_limiter.py {run|start|stop|config} [max_connections]')
        sys.exit(1)
