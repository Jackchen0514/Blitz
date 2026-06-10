#!/usr/bin/env python3
"""
Per-user concurrent connection limiter for Hysteria2.

Architecture (auth-proxy mode):
  Hysteria2 → auth proxy (127.0.0.1:28263) → Go auth server (127.0.0.1:28262)

The proxy rejects new connections at auth time when MAX_CONNECTIONS is exceeded.
No kick needed — the client receives an auth failure and stops reconnecting.
Log monitoring still runs to track active connection counts accurately.
"""

import init_paths
import asyncio
import json
import logging
import os
import re
import sys
from collections import defaultdict
from threading import Lock

import aiohttp
from aiohttp import web
from dotenv import dotenv_values

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
PROXY_PORT = 28263
GO_AUTH_URL = 'http://127.0.0.1:28262/auth'
DEFAULT_MAX_CONNECTIONS = 2

# username → active connection count (updated by log monitor)
_counts: dict[str, int] = defaultdict(int)
_lock = Lock()
_max_conn: int = DEFAULT_MAX_CONNECTIONS

_LOG_RE = re.compile(
    r'(?P<event>client connected|client disconnected)'
    r'.*?"id":\s*"(?P<username>[^"]+)"'
)


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


async def _auth_handler(request: web.Request) -> web.Response:
    """
    Auth proxy handler.
    1. Forward request to Go auth server to validate credentials.
    2. If valid, check connection count before allowing.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'ok': False, 'msg': 'invalid request'}, status=400)

    # Validate credentials via Go auth server
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GO_AUTH_URL, json=data,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f'Go auth server unreachable: {e}')
        return web.json_response({'ok': False, 'msg': 'auth service unavailable'}, status=503)

    if not result.get('ok'):
        return web.json_response(result)

    username = result.get('id', '')
    if not username:
        return web.json_response(result)

    # Check connection limit
    with _lock:
        count = _counts[username]
        if count >= _max_conn and not _is_unlimited(username):
            logger.warning(
                f'{username} rejected — {count}/{_max_conn} connections already active'
            )
            return web.json_response({'ok': False, 'msg': 'connection limit exceeded'})

    logger.info(f'{username} auth OK — active connections: {count}')
    return web.json_response(result)


def _process_log_line(line: str):
    m = _LOG_RE.search(line)
    if not m:
        return
    event = m.group('event')
    username = m.group('username')
    with _lock:
        if event == 'client connected':
            _counts[username] += 1
            logger.info(f'{username} connected — active: {_counts[username]}')
        elif event == 'client disconnected':
            if _counts[username] > 0:
                _counts[username] -= 1
            logger.info(f'{username} disconnected — active: {_counts[username]}')
            if _counts[username] == 0:
                del _counts[username]


async def _monitor_logs():
    proc = await asyncio.create_subprocess_exec(
        'journalctl', '-u', 'hysteria-server.service',
        '-f', '--no-pager', '-o', 'cat',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async for raw in proc.stdout:
        _process_log_line(raw.decode().strip())


async def _run():
    global _max_conn
    _max_conn = _get_max_connections()
    logger.info(
        f'Connection limiter started. '
        f'MAX_CONNECTIONS={_max_conn}, proxy on 127.0.0.1:{PROXY_PORT}'
    )

    app = web.Application()
    app.router.add_post('/auth', _auth_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', PROXY_PORT).start()

    await _monitor_logs()


def _update_config_json(auth_port: int):
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        config['auth'] = {
            'type': 'http',
            'http': {'url': f'http://127.0.0.1:{auth_port}/auth'}
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        print(f'config.json auth URL → http://127.0.0.1:{auth_port}/auth')
    except Exception as e:
        print(f'Warning: could not update config.json: {e}')


def _install_service():
    script_path = os.path.abspath(__file__)
    venv_python = '/etc/hysteria/hysteria2_venv/bin/python'
    unit = f"""[Unit]
Description=Hysteria2 Connection Limiter (auth proxy)
After=network.target
Before=hysteria-server.service

[Service]
Type=simple
ExecStart={venv_python} {script_path} run
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
"""
    with open(f'/etc/systemd/system/{SERVICE_NAME}', 'w') as f:
        f.write(unit)

    _update_config_json(PROXY_PORT)

    os.system('systemctl daemon-reload')
    os.system(f'systemctl enable {SERVICE_NAME}')
    os.system(f'systemctl start {SERVICE_NAME}')
    # Restart hysteria-server so it picks up the new auth URL
    os.system('systemctl restart hysteria-server.service')
    print('Connection limiter service started.')


def _uninstall_service():
    os.system(f'systemctl stop {SERVICE_NAME} 2>/dev/null')
    os.system(f'systemctl disable {SERVICE_NAME} 2>/dev/null')
    unit_file = f'/etc/systemd/system/{SERVICE_NAME}'
    if os.path.exists(unit_file):
        os.remove(unit_file)

    _update_config_json(28262)  # restore direct auth

    os.system('systemctl daemon-reload')
    os.system('systemctl restart hysteria-server.service')
    print('Connection limiter stopped. Hysteria2 restored to direct auth.')


def _set_config(max_connections: int | None):
    env_file = str(CONFIG_ENV)
    if not os.path.exists(env_file):
        print(f'Config file not found: {env_file}')
        return

    key = 'MAX_CONNECTIONS'
    if max_connections is not None:
        lines = open(env_file).readlines()
        updated = False
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

    os.system(f'systemctl is-active --quiet {SERVICE_NAME} && systemctl restart {SERVICE_NAME}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''

    if cmd == 'run':
        asyncio.run(_run())
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
