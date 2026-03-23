import argparse
import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from revpty.server.app import run as run_server
from revpty.client.agent import Agent
from revpty.cli.attach import attach


def convert_to_ws_url(url):
    """Convert http/https URL to ws/wss with /revpty/ws path"""
    url = url.strip()

    # Remove trailing slash
    url = url.rstrip('/')

    # If already ws:// or wss:// with path, return as-is
    if url.startswith('ws://') or url.startswith('wss://'):
        # Add /revpty/ws if no path
        if '/' not in url[5:]:
            return url + '/revpty/ws'
        return url

    # Convert http:// to ws://
    if url.startswith('http://'):
        return url.replace('http://', 'ws://', 1) + '/revpty/ws'

    # Convert https:// to wss://
    if url.startswith('https://'):
        return url.replace('https://', 'wss://', 1) + '/revpty/ws'

    # Default to ws:// if no scheme specified
    if not url.startswith(('http://', 'https://', 'ws://', 'wss://')):
        return f'ws://{url}/revpty/ws'

    return url + '/revpty/ws'


def _resolve_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    if sys.argv[0].endswith(name):
        return sys.argv[0]
    return name


def _install_systemd(service_name: str, exec_args: list[str], user_mode: bool = False):
    if not shutil.which("systemctl"):
        raise SystemExit("systemctl not found; this command is for systemd-based Linux systems only")

    if user_mode:
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        unit_path = os.path.join(unit_dir, f"{service_name}.service")
        wanted_by = "default.target"
    else:
        if os.geteuid() != 0:
            raise SystemExit("run as root to install systemd service (or use --user for user-level)")
        unit_path = f"/etc/systemd/system/{service_name}.service"
        wanted_by = "multi-user.target"

    env_lines = []
    log_level = os.getenv("LOG_LEVEL")
    if log_level:
        env_lines.append(f"Environment=LOG_LEVEL={log_level}")
    unit = "\n".join([
        "[Unit]",
        f"Description={service_name}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        *env_lines,
        f"ExecStart={' '.join(shlex.quote(arg) for arg in exec_args)}",
        "Restart=always",
        "RestartSec=1",
        "",
        "[Install]",
        f"WantedBy={wanted_by}",
        "",
    ])
    with open(unit_path, "w") as f:
        f.write(unit)
    systemctl = ["systemctl", "--user"] if user_mode else ["systemctl"]
    subprocess.run([*systemctl, "daemon-reload"], check=True)
    subprocess.run([*systemctl, "enable", "--now", service_name], check=True)


def server():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--install", action="store_true")
    p.add_argument("--user", action="store_true", help="Install as user-level systemd service")
    p.add_argument("--cache-size", type=int, default=131072, help="Output cache size in bytes (default: 131072 = 128KB)")
    args = p.parse_args()
    if args.install:
        exe = _resolve_executable("revpty-server")
        cmd = [exe, "--host", args.host, "--port", str(args.port)]
        if args.secret:
            cmd += ["--secret", args.secret]
        if args.cache_size != 131072:
            cmd += ["--cache-size", str(args.cache_size)]
        _install_systemd("revpty-server", cmd, user_mode=args.user)
        return
    run_server(args.host, args.port, secret=args.secret, cache_size=args.cache_size)


def client():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", required=True, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--cf-client-id", dest="cf_client_id", default=None, help="Cloudflare Access Client ID")
    p.add_argument("--cf-client-secret", dest="cf_client_secret", default=None, help="Cloudflare Access Client Secret")
    p.add_argument("--exec", default=None, help="Command to execute (e.g. /bin/bash)")
    p.add_argument("--install", action="store_true")
    p.add_argument("--user", action="store_true", help="Install as user-level systemd service")
    args = p.parse_args()

    ws_url = convert_to_ws_url(args.server)
    if args.install:
        exe = _resolve_executable("revpty-client")
        cmd = [exe, "--server", args.server, "--session", args.session]
        if args.proxy:
            cmd += ["--proxy", args.proxy]
        if args.secret:
            cmd += ["--secret", args.secret]
        if args.cf_client_id:
            cmd += ["--cf-client-id", args.cf_client_id]
        if args.cf_client_secret:
            cmd += ["--cf-client-secret", args.cf_client_secret]
        if args.exec:
            cmd += ["--exec", args.exec]
        _install_systemd("revpty-client", cmd, user_mode=args.user)
        return

    shell = args.exec or "/bin/bash"
    asyncio.run(Agent(ws_url, args.session, shell=shell, proxy=args.proxy, secret=args.secret,
                      cf_client_id=args.cf_client_id, cf_client_secret=args.cf_client_secret).run())


def attach_cmd():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", required=True, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--cf-client-id", dest="cf_client_id", default=None, help="Cloudflare Access Client ID")
    p.add_argument("--cf-client-secret", dest="cf_client_secret", default=None, help="Cloudflare Access Client Secret")
    args = p.parse_args()

    ws_url = convert_to_ws_url(args.server)
    asyncio.run(attach(ws_url, args.session, proxy=args.proxy, secret=args.secret,
                       cf_client_id=args.cf_client_id, cf_client_secret=args.cf_client_secret))
