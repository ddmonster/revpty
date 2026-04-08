import argparse
import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import json
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from revpty.server.app import run as run_server
from revpty.client.agent import Agent
from revpty.cli.attach import attach
from revpty.platform_utils import IS_WINDOWS, default_shell


def load_config(config_path: str) -> dict:
    """Load config from TOML or JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    content = path.read_text()
    if path.suffix in (".toml",):
        return tomllib.loads(content)
    elif path.suffix in (".json",):
        return json.loads(content)
    else:
        # Try TOML first, then JSON
        try:
            return tomllib.loads(content)
        except Exception:
            return json.loads(content)


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


def _install_systemd(service_name: str, exec_args: list[str], user_mode: bool = False, config_path: str = None):
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

    # If config file is specified, use it and simplify ExecStart
    if config_path:
        exec_start = f"ExecStart={shlex.quote(exec_args[0])} --config {shlex.quote(config_path)}"
    else:
        exec_start = f"ExecStart={' '.join(shlex.quote(arg) for arg in exec_args)}"

    unit = "\n".join([
        "[Unit]",
        f"Description={service_name}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        *env_lines,
        exec_start,
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
    p.add_argument("--config", help="Load settings from TOML or JSON config file")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--install", action="store_true")
    p.add_argument("--user", action="store_true", help="Install as user-level systemd service")
    p.add_argument("--cache-size", type=int, default=None, help="Output cache size in bytes (default: 131072 = 128KB)")
    args = p.parse_args()

    # Load config file if provided
    config = {}
    if args.config:
        config = load_config(args.config)

    # Merge CLI args over config file values
    host = args.host or config.get("host", "0.0.0.0")
    port = args.port or config.get("port", 8765)
    secret = args.secret if args.secret is not None else config.get("secret")
    cache_size = args.cache_size or config.get("cache_size", 131072)

    if args.install:
        if IS_WINDOWS:
            raise SystemExit("--install is not supported on Windows")
        exe = _resolve_executable("revpty-server")
        cmd = [exe, "--host", host, "--port", str(port)]
        if secret:
            cmd += ["--secret", secret]
        if cache_size != 131072:
            cmd += ["--cache-size", str(cache_size)]
        _install_systemd("revpty-server", cmd, user_mode=args.user, config_path=args.config)
        return
    run_server(host, port, secret=secret, cache_size=cache_size)


def client():
    p = argparse.ArgumentParser()
    p.add_argument("--config", help="Load settings from TOML or JSON config file")
    p.add_argument("--server", default=None, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", default=None, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--cf-client-id", dest="cf_client_id", default=None, help="Cloudflare Access Client ID")
    p.add_argument("--cf-client-secret", dest="cf_client_secret", default=None, help="Cloudflare Access Client Secret")
    p.add_argument("--exec", default=None, help="Command to execute (e.g. /bin/bash)")
    p.add_argument("--tunnel", action="append", default=None, help="Register HTTP tunnel (format: port or host:port)", metavar="PORT")
    p.add_argument("--insecure", action="store_true", help="Skip SSL certificate verification")
    p.add_argument("--install", action="store_true")
    p.add_argument("--user", action="store_true", help="Install as user-level systemd service")
    args = p.parse_args()

    # Load config file if provided
    config = {}
    if args.config:
        config = load_config(args.config)

    # Merge CLI args over config file values
    server_url = args.server or config.get("server")
    session = args.session or config.get("session")
    if not server_url or not session:
        p.error("--server and --session are required (or use --config)")

    proxy = args.proxy if args.proxy is not None else config.get("proxy")
    secret = args.secret if args.secret is not None else config.get("secret")
    cf_client_id = args.cf_client_id or config.get("cf_client_id")
    cf_client_secret = args.cf_client_secret or config.get("cf_client_secret")
    shell = args.exec or config.get("exec", default_shell())
    tunnels = args.tunnel if args.tunnel is not None else config.get("tunnels", [])
    insecure = args.insecure or config.get("insecure", False)

    ws_url = convert_to_ws_url(server_url)
    if args.install:
        if IS_WINDOWS:
            raise SystemExit("--install is not supported on Windows")
        exe = _resolve_executable("revpty-client")
        cmd = [exe, "--server", server_url, "--session", session]
        if proxy:
            cmd += ["--proxy", proxy]
        if secret:
            cmd += ["--secret", secret]
        if cf_client_id:
            cmd += ["--cf-client-id", cf_client_id]
        if cf_client_secret:
            cmd += ["--cf-client-secret", cf_client_secret]
        if shell != default_shell():
            cmd += ["--exec", shell]
        for t in tunnels:
            cmd += ["--tunnel", t]
        if insecure:
            cmd += ["--insecure"]
        _install_systemd("revpty-client", cmd, user_mode=args.user, config_path=args.config)
        return

    asyncio.run(Agent(ws_url, session, shell=shell, proxy=proxy, secret=secret,
                      cf_client_id=cf_client_id, cf_client_secret=cf_client_secret,
                      insecure=insecure, tunnels=tunnels).run())


def attach_cmd():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", required=True, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", dest="secret", default=None)
    p.add_argument("--cf-client-id", dest="cf_client_id", default=None, help="Cloudflare Access Client ID")
    p.add_argument("--cf-client-secret", dest="cf_client_secret", default=None, help="Cloudflare Access Client Secret")
    p.add_argument("--insecure", action="store_true", help="Skip SSL certificate verification")
    args = p.parse_args()

    ws_url = convert_to_ws_url(args.server)
    asyncio.run(attach(ws_url, args.session, proxy=args.proxy, secret=args.secret,
                       cf_client_id=args.cf_client_id, cf_client_secret=args.cf_client_secret,
                       insecure=args.insecure))
