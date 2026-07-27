#!/usr/bin/env python3
"""
Per-user concurrent connection limiter for Hysteria2.

Architecture (auth-proxy mode):
  Hysteria2 → auth proxy (127.0.0.1:28263) → Go auth server (127.0.0.1:28262)

Connection count is incremented atomically inside the auth handler (under lock)
so simultaneous reconnects cannot both slip through. The journal monitor only
handles 'client disconnected' events (decrement). On startup, up to 4 hours of
journal history is replayed to restore current state; users exceeding the new
MAX_CONNECTIONS are kicked so they re-authenticate under the updated limit.

On every startup, config.json is verified to point to the proxy port. If it was
reset (e.g. by upgrade.sh), it is corrected and hysteria-server is restarted.
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
PROXY_PORT = 28263
GO_AUTH_PORT = 28262
GO_AUTH_URL = f'http://127.0.0.1:{GO_AUTH_PORT}/auth'
TRAFFIC_API_URL = 'http://127.0.0.1:25413'
DEFAULT_MAX_CONNECTIONS = 2

_counts: dict[str, int] = defaultdict(int)
_lock = Lock()
_max_conn: int = DEFAULT_MAX_CONNECTIONS


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


def _get_secret() -> str | None:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get('trafficStats', {}).get('secret')
    except Exception:
        return None


def _kick_users(usernames: list[str]):
    secret = _get_secret()
    if not secret:
        logger.error('Cannot kick users: trafficStats.secret not found.')
        return
    try:
        Hysteria2Client(base_url=TRAFFIC_API_URL, secret=secret).kick_clients(usernames)
        logger.warning(f'Kicked users exceeding new limit: {usernames}')
    except Exception as e:
        logger.error(f'Kick failed: {e}')


_LOG_RE = re.compile(
    r'(?P<event>client connected|client disconnected)'
    r'.*?"id":\s*"(?P<username>[^"]+)"'
)


def _process_disconnect(line: str):
    """Only handle disconnect events — connect counts are managed by the auth handler."""
    m = _LOG_RE.search(line)
    if not m or m.group('event') != 'client disconnected':
        return
    username = m.group('username')
    with _lock:
        if _counts[username] > 0:
            _counts[username] -= 1
        if _counts[username] == 0:
            del _counts[username]
        logger.info(f'{username} disconnected — active: {_counts.get(username, 0)}')


def _process_history_line(line: str):
    """Replay both connect and disconnect events when reconstructing state from journal."""
    m = _LOG_RE.search(line)
    if not m:
        return
    event = m.group('event')
    username = m.group('username')
    with _lock:
        if event == 'client connected':
            _counts[username] += 1
        elif event == 'client disconnected':
            if _counts[username] > 0:
                _counts[username] -= 1
            if _counts[username] == 0:
                del _counts[username]


async def _get_server_start_time() -> str:
    """Return the last start timestamp of hysteria-server as a journalctl --since string."""
    p = await asyncio.create_subprocess_exec(
        'systemctl', 'show', 'hysteria-server.service',
        '--property=ActiveEnterTimestamp',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await p.communicate()
    for line in out.decode().splitlines():
        if line.startswith('ActiveEnterTimestamp='):
            ts = line.split('=', 1)[1].strip()
            # Format: "Sun 2026-07-27 05:00:00 UTC" — extract date+time for journalctl
            parts = ts.split()
            if len(parts) >= 3 and parts[1] != 'n/a':
                return f'{parts[1]} {parts[2]}'
    return '4 hours ago'  # fallback if service not started yet


async def _init_counts():
    """
    Replay hysteria-server journal since its last start time to reconstruct
    current connection state.

    Using the server's actual start time (not a fixed window) is critical:
    hysteria-server terminates all connections on restart without writing
    'client disconnected' events, so replaying a fixed window accumulates
    stale connect events that never have matching disconnects.
    """
    since = await _get_server_start_time()
    logger.info(f'Replaying hysteria-server journal since: {since}')

    p = await asyncio.create_subprocess_exec(
        'journalctl', '-u', 'hysteria-server.service',
        '--since', since, '--no-pager', '-o', 'cat',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await p.communicate()
    for line in out.decode().splitlines():
        _process_history_line(line.strip())

    if _counts:
        logger.info(f'Restored connection state from journal: {dict(_counts)}')
    else:
        logger.info('No active connections found in journal history.')


def _ensure_auth_proxy_config() -> bool:
    """
    Verify config.json auth URL points to our proxy (port 28263).
    Returns True if the config was wrong and was updated (caller must restart
    hysteria-server so it picks up the corrected URL).

    This self-healing is needed because upgrade.sh always resets the auth URL
    to port 28262 (direct Go auth server).
    """
    expected_url = f'http://127.0.0.1:{PROXY_PORT}/auth'
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        current_url = config.get('auth', {}).get('http', {}).get('url', '')
        if current_url == expected_url:
            return False
        logger.warning(
            f'config.json auth URL is {current_url!r} instead of {expected_url!r} — fixing.'
        )
        _update_config_json(PROXY_PORT)
        return True
    except Exception as e:
        logger.error(f'Could not check/fix config.json: {e}')
        return False


async def _auth_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'ok': False, 'msg': 'invalid request'}, status=400)

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

    with _lock:
        count = _counts[username]
        if count >= _max_conn and not _is_unlimited(username):
            logger.warning(
                f'{username} rejected — {count}/{_max_conn} connections already active'
            )
            return web.json_response({'ok': False, 'msg': 'connection limit exceeded'})
        # Increment here, inside the lock, before returning OK.
        # This prevents simultaneous reconnects from both seeing count=0.
        _counts[username] += 1

    logger.info(f'{username} auth OK — active connections: {count + 1}')
    return web.json_response(result)


async def _monitor_logs():
    """Follow live journal; only disconnect events are handled (connect is counted in auth handler)."""
    proc = await asyncio.create_subprocess_exec(
        'journalctl', '-u', 'hysteria-server.service',
        '-f', '-n', '0', '--no-pager', '-o', 'cat',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async for raw in proc.stdout:
        _process_disconnect(raw.decode().strip())


async def _run():
    global _max_conn
    _max_conn = _get_max_connections()
    logger.info(
        f'Connection limiter started. MAX_CONNECTIONS={_max_conn}, '
        f'proxy on 127.0.0.1:{PROXY_PORT}'
    )

    # Ensure hysteria-server uses our proxy. If config.json was reset by
    # upgrade.sh (which always writes port 28262), fix it and restart the server.
    # When the server restarts all connections are terminated, so we start fresh.
    config_was_wrong = _ensure_auth_proxy_config()
    if config_was_wrong:
        logger.info('Restarting hysteria-server to apply corrected auth URL...')
        proc = await asyncio.create_subprocess_exec(
            'systemctl', 'restart', 'hysteria-server.service',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(2)
        logger.info('hysteria-server restarted — all prior connections terminated, counts start at 0.')
    else:
        # Normal restart (config already correct): restore state from journal.
        await _init_counts()

        # Kick and clear users who exceed the (possibly newly lowered) limit so
        # they re-authenticate through the proxy with the new limit applied.
        to_kick = []
        with _lock:
            for username, count in list(_counts.items()):
                if count > _max_conn and not _is_unlimited(username):
                    logger.warning(
                        f'{username} has {count} active connections, exceeds new limit {_max_conn} — kicking.'
                    )
                    to_kick.append(username)
                    del _counts[username]
        if to_kick:
            _kick_users(to_kick)

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
    os.system('systemctl restart hysteria-server.service')
    print('Connection limiter service started.')


def _uninstall_service():
    os.system(f'systemctl stop {SERVICE_NAME} 2>/dev/null')
    os.system(f'systemctl disable {SERVICE_NAME} 2>/dev/null')
    unit_file = f'/etc/systemd/system/{SERVICE_NAME}'
    if os.path.exists(unit_file):
        os.remove(unit_file)

    _update_config_json(GO_AUTH_PORT)

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
