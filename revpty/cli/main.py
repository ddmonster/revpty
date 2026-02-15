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
    """Convert http/https URL to ws/wss"""
    url = url.strip()
    
    # If already ws:// or wss://, return as-is
    if url.startswith('ws://') or url.startswith('wss://'):
        return url
    
    # Convert http:// to ws://
    if url.startswith('http://'):
        return url.replace('http://', 'ws://', 1)
    
    # Convert https:// to wss://
    if url.startswith('https://'):
        return url.replace('https://', 'wss://', 1)
    
    # Default to ws:// if no scheme specified
    if not url.startswith(('http://', 'https://', 'ws://', 'wss://')):
        return f'ws://{url}'
    
    return url


def _resolve_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    if sys.argv[0].endswith(name):
        return sys.argv[0]
    return name


def _install_systemd(service_name: str, exec_args: list[str]):
    if os.geteuid() != 0:
        raise SystemExit("run as root to install systemd service")
    
    if not shutil.which("systemctl"):
        raise SystemExit("systemctl not found; this command is for systemd-based Linux systems only")
        
    unit_path = f"/etc/systemd/system/{service_name}.service"
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
        "WantedBy=multi-user.target",
        "",
    ])
    with open(unit_path, "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", service_name], check=True)


def server():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--secret", "--seceret", dest="secret", default=None)
    p.add_argument("--install", action="store_true")
    args = p.parse_args()
    if args.install:
        exe = _resolve_executable("revpty-server")
        cmd = [exe, "--host", args.host, "--port", str(args.port)]
        if args.secret:
            cmd += ["--secret", args.secret]
        _install_systemd("revpty-server", cmd)
        return
    run_server(args.host, args.port, secret=args.secret)


def client():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", required=True, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", "--seceret", dest="secret", default=None)
    p.add_argument("--exec", default=None, help="Command to execute (e.g. /bin/bash)")
    p.add_argument("--install", action="store_true")
    args = p.parse_args()
    
    ws_url = convert_to_ws_url(args.server)
    if args.install:
        exe = _resolve_executable("revpty-client")
        cmd = [exe, "--server", args.server, "--session", args.session]
        if args.proxy:
            cmd += ["--proxy", args.proxy]
        if args.secret:
            cmd += ["--secret", args.secret]
        if args.exec:
            cmd += ["--exec", args.exec]
        _install_systemd("revpty-client", cmd)
        return
    
    shell = args.exec or "/bin/bash"
    asyncio.run(Agent(ws_url, args.session, shell=shell, proxy=args.proxy, secret=args.secret).run())


def attach_cmd():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="Server URL (auto-converts http/https to ws/wss)")
    p.add_argument("--session", required=True, help="Session name")
    p.add_argument("--proxy", default=None, help="HTTP proxy URL")
    p.add_argument("--secret", "--seceret", dest="secret", default=None)
    args = p.parse_args()
    
    ws_url = convert_to_ws_url(args.server)
    asyncio.run(attach(ws_url, args.session, proxy=args.proxy, secret=args.secret))
