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
from datetime import datetime, timezone
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
_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')

_log_file: str = ''  # set during startup; empty = use systemd journal


async def _process_disconnect(line: str):
    """Handle disconnect events — decrement fallback counter and log real API count."""
    m = _LOG_RE.search(line)
    if not m or m.group('event') != 'client disconnected':
        return
    username = m.group('username')
    with _lock:
        if _counts[username] > 0:
            _counts[username] -= 1
        if _counts[username] == 0:
            del _counts[username]

    live_count = await _get_live_connection_count(username)
    if live_count >= 0:
        logger.info(f'{username} disconnected — active: {live_count}')
    else:
        logger.info(f'{username} disconnected — active: {_counts.get(username, 0)} (fallback)')


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


async def _get_server_start_time() -> tuple[str, datetime | None]:
    """
    Return the last start time of hysteria-server as:
      - a journalctl --since string (e.g. "2026-07-27 06:12:21")
      - a timezone-aware datetime (for file-based log filtering)
    Falls back to ('4 hours ago', None) if unavailable.
    """
    p = await asyncio.create_subprocess_exec(
        'systemctl', 'show', 'hysteria-server.service',
        '--property=ActiveEnterTimestamp',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await p.communicate()
    for line in out.decode().splitlines():
        if line.startswith('ActiveEnterTimestamp='):
            ts = line.split('=', 1)[1].strip()
            parts = ts.split()
            if len(parts) >= 3 and parts[1] != 'n/a':
                since_str = f'{parts[1]} {parts[2]}'
                try:
                    dt = datetime.strptime(since_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                    return since_str, dt
                except Exception:
                    return since_str, None
    return '4 hours ago', None


def _detect_log_source() -> str:
    """
    Check if hysteria-server redirects stdout to a file via a drop-in override.
    Parses unit files directly — systemctl show strips the path from StandardOutput,
    returning only the mode (e.g. 'append' instead of 'append:/var/log/...').
    Returns the log file path if found, else '' (use systemd journal).
    """
    dropin_dir = '/etc/systemd/system/hysteria-server.service.d'
    candidates = []
    if os.path.isdir(dropin_dir):
        for fname in sorted(os.listdir(dropin_dir)):
            if fname.endswith('.conf'):
                candidates.append(os.path.join(dropin_dir, fname))
    candidates.append('/etc/systemd/system/hysteria-server.service')

    for unit_file in candidates:
        try:
            with open(unit_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('StandardOutput=') and ':' in line:
                        val = line.split('=', 1)[1].strip()
                        prefix, log_path = val.split(':', 1)
                        if prefix in ('append', 'file') and os.path.exists(log_path):
                            return log_path
        except Exception:
            pass
    return ''


async def _init_counts():
    """
    Replay hysteria-server logs since its last start time to reconstruct
    current connection state. Supports both file-based and journal logging.
    """
    since_str, since_dt = await _get_server_start_time()
    logger.info(f'Replaying hysteria-server logs since: {since_str}')

    if _log_file:
        try:
            with open(_log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if since_dt:
                        m = _TS_RE.match(line)
                        if not m:
                            continue
                        try:
                            line_dt = datetime.fromisoformat(m.group(1) + '+00:00')
                            if line_dt < since_dt:
                                continue
                        except Exception:
                            continue
                    _process_history_line(line)
        except Exception as e:
            logger.warning(f'Could not read log file {_log_file}: {e}')
    else:
        p = await asyncio.create_subprocess_exec(
            'journalctl', '-u', 'hysteria-server.service',
            '--since', since_str, '--no-pager', '-o', 'cat',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        for line in out.decode().splitlines():
            _process_history_line(line.strip())

    if _counts:
        logger.info(f'Restored connection state: {dict(_counts)}')
    else:
        logger.info('No active connections found in history.')


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


async def _get_live_connection_count(username: str) -> int:
    """
    Query the hysteria2 traffic API for the real number of live QUIC connections.

    This is the authoritative source: it reflects actual server-side state and
    automatically corrects for network switches (WiFi↔cellular) where the old
    QUIC connection hasn't timed out yet but the client has already reconnected.

    Returns -1 if the API is unreachable (caller falls back to internal counter).
    """
    secret = _get_secret()
    if not secret:
        return -1
    loop = asyncio.get_event_loop()
    try:
        client = Hysteria2Client(base_url=TRAFFIC_API_URL, secret=secret)
        online = await loop.run_in_executor(None, client.get_online_clients)
        user_status = online.get(username)
        if not user_status or not getattr(user_status, 'is_online', False):
            return 0
        connections = getattr(user_status, 'connections', None)
        if connections is None:
            return 1
        try:
            return len(connections)
        except TypeError:
            return int(connections) if isinstance(connections, int) else 1
    except Exception as e:
        logger.warning(f'Traffic API unavailable for {username}: {e}')
        return -1


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

    if _is_unlimited(username):
        return web.json_response(result)

    # Primary: use hysteria2 traffic API for the real live connection count.
    # This handles network switches correctly — the API reflects actual QUIC
    # state and doesn't accumulate stale counts like a journal-based counter.
    live_count = await _get_live_connection_count(username)

    if live_count >= 0:
        if live_count >= _max_conn:
            logger.warning(f'{username} rejected — {live_count}/{_max_conn} connections already active')
            return web.json_response({'ok': False, 'msg': 'connection limit exceeded'})
        logger.info(f'{username} auth OK — {live_count + 1} connections after this')
    else:
        # Fallback: traffic API unavailable, use internal counter.
        with _lock:
            count = _counts[username]
            if count >= _max_conn:
                logger.warning(f'{username} rejected — {count}/{_max_conn} connections already active (fallback counter)')
                return web.json_response({'ok': False, 'msg': 'connection limit exceeded'})
            _counts[username] += 1
        logger.info(f'{username} auth OK — active connections: {_counts[username]} (fallback counter)')

    return web.json_response(result)


async def _monitor_logs():
    """Follow live hysteria-server logs; only disconnect events are handled."""
    if _log_file:
        proc = await asyncio.create_subprocess_exec(
            'tail', '-F', '-n', '0', _log_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            'journalctl', '-u', 'hysteria-server.service',
            '-f', '-n', '0', '--no-pager', '-o', 'cat',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    async for raw in proc.stdout:
        await _process_disconnect(raw.decode().strip())


async def _run():
    global _max_conn, _log_file
    _max_conn = _get_max_connections()
    _log_file = _detect_log_source()
    logger.info(
        f'Connection limiter started. MAX_CONNECTIONS={_max_conn}, '
        f'proxy on 127.0.0.1:{PROXY_PORT}'
    )
    if _log_file:
        logger.info(f'Log source: file ({_log_file})')
    else:
        logger.info('Log source: systemd journal')

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
