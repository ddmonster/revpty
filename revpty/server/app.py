import logging
import asyncio
import json
import time
import os
import hmac
import random
import uuid
from pathlib import Path
from dataclasses import dataclass
from aiohttp import web
from aiohttp import WSMsgType
from revpty.protocol.codec import decode, encode, ProtocolError
from revpty.protocol.frame import Frame, FrameType, Role
from revpty.session import SessionManager, SessionConfig
from revpty.client.pty_shell import PTYShell
from revpty.server.tunnel import TunnelManager
from revpty import __version__
_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
SECRET_KEY = web.AppKey("revpty_secret", str)


def pty_factory(shell):
    """Factory function to create PTY instances"""
    return PTYShell(shell)


# Global Session Manager
session_manager = None

# Share store for share links
@dataclass
class ShareRecord:
    id: str           # 8-digit numeric
    session_id: str
    mode: str         # "ro" or "rw"
    secret: str       # original secret for proxy auth
    created_at: float

share_store: dict[str, ShareRecord] = {}

# Tunnel manager
tunnel_manager = TunnelManager()


def is_valid_tunnel_id(s: str) -> bool:
    """Check if string looks like a tunnel_id (8-char hex)"""
    if len(s) != 8:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def get_tunnel_id(request) -> str | None:
    """Get tunnel_id from header or cookie (priority order)."""
    # 1. Check header
    tunnel_id = request.headers.get("X-Tunnel-Id")
    if tunnel_id:
        return tunnel_id

    # 2. Check cookie
    return request.cookies.get("tunnel_id")

async def print_status():
    """Periodically print active sessions"""
    while True:
        await asyncio.sleep(30)
        if session_manager and session_manager.sessions:
            active = list(session_manager.sessions.keys())
            logger.info(f"[*] Active sessions: {', '.join(sorted(active))}")

STATIC_DIR = Path(__file__).parent / "static"

async def gui_handler(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def sessions_api_handler(request):
    """Return active sessions list"""
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.json_response({"error": "unauthorized"}, status=401)
            
    sessions_data = []
    if session_manager:
        for sid, session in session_manager.sessions.items():
            sessions_data.append({
                "id": sid,
                "clients": len(session.clients),
                "browsers": len(session.browsers),
                "state": session.state.value,
                "active_at": int(session.last_active),
                "created_at": int(session.created_at),
                "shell": session.config.shell
            })
    
    # Sort by activity (newest first)
    sessions_data.sort(key=lambda x: x["active_at"], reverse=True)
    return web.json_response(sessions_data)


async def create_share_handler(request):
    """Create a share link for a session"""
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    session_id = body.get("session_id")
    mode = body.get("mode", "ro")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    if mode not in ("ro", "rw"):
        return web.json_response({"error": "mode must be 'ro' or 'rw'"}, status=400)

    # Check session exists
    if session_manager and session_id not in session_manager.sessions:
        return web.json_response({"error": "session not found"}, status=404)

    # Generate unique 8-digit ID
    for _ in range(100):
        share_id = str(random.randint(10000000, 99999999))
        if share_id not in share_store:
            break
    else:
        return web.json_response({"error": "failed to generate unique share id"}, status=500)

    share_store[share_id] = ShareRecord(
        id=share_id,
        session_id=session_id,
        mode=mode,
        secret=required_secret or "",
        created_at=time.time(),
    )

    url = f"/revpty/share/{share_id}"
    return web.json_response({"id": share_id, "url": url})


async def resolve_share_handler(request):
    """Resolve a share link and redirect to GUI"""
    share_id = request.match_info["share_id"]
    record = share_store.get(share_id)
    if not record:
        return web.Response(
            text="<html><body style='background:#0b0e11;color:#e6e6e6;font-family:sans-serif;padding:40px;text-align:center'>"
                 "<h1>Share link not found</h1><p>This share link is invalid or has expired.</p></body></html>",
            content_type="text/html",
            status=404,
        )

    params = f"session={record.session_id}"
    if record.secret:
        params += f"&secret={record.secret}"
    if record.mode == "ro":
        params += "&mode=ro"

    raise web.HTTPFound(f"/revpty/gui?{params}")


async def tunnel_api_handler(request):
    """API to manage HTTP tunnel mappings."""
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.json_response({"error": "unauthorized"}, status=401)

    if request.method == "GET":
        session_id = request.query.get("session")
        mappings = tunnel_manager.list_mappings(session_id)
        return web.json_response([
            {"id": m.id, "tunnel_id": m.tunnel_id, "session_id": m.session_id,
             "local_host": m.local_host, "local_port": m.local_port,
             "url": f"/{m.tunnel_id}"}
            for m in mappings
        ])

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        session_id = body.get("session_id")
        local_port = body.get("local_port")
        local_host = body.get("local_host", "127.0.0.1")

        if not session_id or not local_port:
            return web.json_response({"error": "session_id and local_port required"}, status=400)

        mapping = tunnel_manager.add_mapping(session_id, local_port, local_host)
        return web.json_response({"id": mapping.id, "tunnel_id": mapping.tunnel_id,
                                   "local_host": mapping.local_host, "local_port": mapping.local_port,
                                   "url": f"/{mapping.tunnel_id}"})


async def tunnel_delete_handler(request):
    """Delete a tunnel mapping."""
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.json_response({"error": "unauthorized"}, status=401)

    tunnel_id = request.match_info["tunnel_id"]
    tunnel_manager.remove_mapping(tunnel_id)
    return web.json_response({"ok": True})


async def proxy_tunnel_request(request, tunnel_id: str, path: str):
    """Common proxy logic for tunnel requests."""
    mapping = tunnel_manager.get_mapping_by_tunnel_id(tunnel_id)
    if not mapping:
        return web.Response(status=404, text="Tunnel not found")

    session = session_manager.get(mapping.session_id) if session_manager else None
    if not session or not session.clients:
        return web.Response(status=502, text="No connected client")

    request_id = uuid.uuid4().hex[:12]
    body = await request.read()
    headers = dict(request.headers)

    if request.query_string:
        path += "?" + request.query_string

    result = await tunnel_manager.proxy_request(
        session, request_id, request.method, path, headers, body, mapping
    )

    status = result.get("status", 502)
    resp_headers = result.get("headers", {})
    resp_body = result.get("body", b"")

    if isinstance(resp_body, str):
        resp_body = resp_body.encode()

    # If body_b64 is hex-encoded
    if "body_b64" in result and result["body_b64"]:
        try:
            resp_body = bytes.fromhex(result["body_b64"])
        except ValueError:
            pass

    # Filter response headers
    skip_headers = {"transfer-encoding", "connection", "content-encoding"}
    filtered = {k: v for k, v in resp_headers.items() if k.lower() not in skip_headers}

    response = web.Response(status=status, headers=filtered, body=resp_body)

    # Set cookies to enable tunnel mode for subsequent requests
    response.set_cookie("tunnel_id", tunnel_id, path="/", max_age=3600)

    return response


async def tunnel_clear_handler(request):
    """Clear tunnel cookies to exit tunnel mode."""
    response = web.json_response({"ok": True})
    response.del_cookie("tunnel_id", path="/")
    return response


async def unified_handler(request):
    """
    Unified catch-all handler for tunnel requests.
    Resolves tunnel_id from URL path, header, or cookie (priority order).
    """
    path = request.match_info.get("path", "")

    # 1. Check if first path segment is a valid tunnel_id (highest priority)
    parts = path.split("/", 1)
    first_part = parts[0]

    if first_part and is_valid_tunnel_id(first_part):
        mapping = tunnel_manager.get_mapping_by_tunnel_id(first_part)
        if mapping:
            remaining = "/" + parts[1] if len(parts) > 1 else "/"
            return await proxy_tunnel_request(request, first_part, remaining)

    # 2. Check header X-Tunnel-Id
    tunnel_id = request.headers.get("X-Tunnel-Id")
    if tunnel_id:
        mapping = tunnel_manager.get_mapping_by_tunnel_id(tunnel_id)
        if mapping:
            return await proxy_tunnel_request(request, tunnel_id, "/" + path)
        return web.Response(status=404, text="Tunnel not found")

    # 3. Check cookie tunnel_id
    tunnel_id = request.cookies.get("tunnel_id")
    if tunnel_id:
        mapping = tunnel_manager.get_mapping_by_tunnel_id(tunnel_id)
        if mapping:
            return await proxy_tunnel_request(request, tunnel_id, "/" + path)

    return web.Response(status=404, text="Not found")


async def broadcast_status(session):
    """Broadcast session status to all peers"""
    # Notify browsers (peers = count of clients)
    browser_peers = len(session.clients)
    browser_payload = json.dumps({
        "session": session.id,
        "role": "browser",
        "peers": browser_peers,
        "state": session.state.value,
    }).encode("utf-8")
    
    browser_frame = encode(Frame(
        session=session.id,
        role="server",
        type=FrameType.STATUS.value,
        data=browser_payload,
    ))
    
    for browser in session.browsers:
        if not browser.closed:
            await browser.send_str(browser_frame)
            
    # Notify clients (peers = count of browsers)
    client_peers = len(session.browsers)
    client_payload = json.dumps({
        "session": session.id,
        "role": "client",
        "peers": client_peers,
        "state": session.state.value,
    }).encode("utf-8")
    
    client_frame = encode(Frame(
        session=session.id,
        role="server",
        type=FrameType.STATUS.value,
        data=client_payload,
    ))
    
    for client in session.clients:
        if not client.closed:
            await client.send_str(client_frame)


async def websocket_handler(request):
    """Handle WebSocket connections with Session-based routing"""
    ws = web.WebSocketResponse(heartbeat=30, compress=True)
    can_prepare = ws.can_prepare(request)
    if not can_prepare.ok:
        return web.Response(text="revpty server", content_type="text/plain")
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.Response(status=401, text="unauthorized")
    await ws.prepare(request)
    remote_addr = request.remote or "unknown"
    
    logger.info(f"[+] Connection from {remote_addr}")
    
    current_session = None
    current_role = None
    
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    # Decode and validate frame
                    frame = decode(msg.data)
                    current_session = frame.session
                    current_role = frame.role
                    
                    # Handle attach/detach
                    if frame.type == FrameType.ATTACH.value:
                        session = await session_manager.attach(
                            frame.session, frame.role, ws
                        )
                        logger.info(f"[+] {frame.role.upper()} attached to '{frame.session}'")
                        # Replay cached output to newly attached browser/viewer
                        if frame.role in ("browser", "viewer") and session.output_buffer:
                            cached = session.output_buffer.get_all()
                            replay_frame = encode(Frame(
                                session=frame.session,
                                role="server",
                                type=FrameType.OUTPUT.value,
                                data=cached,
                            ))
                            await ws.send_str(replay_frame)
                        await broadcast_status(session)
                        continue
                    
                    # Handle detach
                    if frame.type == FrameType.DETACH.value:
                        session_manager.detach(frame.session, frame.role, ws)
                        logger.info(f"[-] {frame.role.upper()} detached from '{frame.session}'")
                        session = session_manager.get(frame.session)
                        if session:
                            await broadcast_status(session)
                        continue

                    # Handle PING from client - respond with PONG (before session check)
                    if frame.type == FrameType.PING.value and frame.role == Role.CLIENT.value:
                        pong_frame = encode(Frame(
                            session=frame.session,
                            role="server",
                            type=FrameType.PONG.value,
                        ))
                        await ws.send_str(pong_frame)
                        continue

                    # Route data frames through session
                    session = session_manager.get(frame.session)
                    if not session:
                        logger.warning(f"[!] Session '{frame.session}' not found")
                        continue
                    
                    # Update session activity
                    session.last_active = time.time()
                    
                    # Security: Enforce read-only for Viewer role
                    if frame.role == Role.VIEWER.value:
                        if frame.type in (FrameType.INPUT.value, FrameType.RESIZE.value):
                            # Viewers cannot send input or resize
                            continue
                        if frame.type == FrameType.FILE.value:
                            # Viewers can only list/read, not write
                            try:
                                file_op = json.loads(frame.data)
                                if file_op.get("op") not in ("list", "read"):
                                    continue
                            except:
                                continue

                    control_payload = None
                    if frame.type == FrameType.CONTROL.value and frame.data:
                        try:
                            control_payload = json.loads(frame.data)
                        except Exception:
                            control_payload = None

                    # Get peers and broadcast
                    peers = session.get_peer(frame.role)
                    if peers:
                        logger.debug(f"[>] {frame.session}: {frame.role} -> {frame.type} ({len(peers)} peers)")
                        for peer in list(peers):  # Copy to avoid modification during iteration
                            if not peer.closed:
                                await peer.send_str(msg.data)
                    elif frame.type not in (FrameType.PING.value, FrameType.PONG.value):
                        logger.debug(f"[!] No peers for {frame.session} {frame.role} -> {frame.type}")

                    # Cache OUTPUT frames from client for browser replay
                    if frame.type == FrameType.OUTPUT.value and frame.role == Role.CLIENT.value:
                        session.output_buffer.append(frame.data)

                    if (
                        control_payload
                        and frame.role == Role.CLIENT.value
                        and control_payload.get("op") == "close_shell_ack"
                    ):
                        await session_manager.close_session(frame.session)

                    # Handle tunnel responses from client (Phase 7)
                    if (
                        control_payload
                        and frame.role == Role.CLIENT.value
                        and control_payload.get("op") == "tunnel_response"
                    ):
                        tunnel_manager.handle_response(
                            control_payload.get("request_id", ""),
                            control_payload
                        )

                    # Handle tunnel registration from client (auto-reconnect)
                    if (
                        control_payload
                        and frame.role == Role.CLIENT.value
                        and control_payload.get("op") == "tunnel_register"
                    ):
                        local_port = control_payload.get("local_port")
                        local_host = control_payload.get("local_host", "127.0.0.1")
                        if local_port:
                            mapping = tunnel_manager.add_mapping(
                                frame.session, local_port, local_host
                            )
                            # Send ack back to client
                            ack_frame = encode(Frame(
                                session=frame.session,
                                role="server",
                                type=FrameType.CONTROL.value,
                                data=json.dumps({
                                    "op": "tunnel_register_ack",
                                    "tunnel_id": mapping.tunnel_id,
                                    "local_port": local_port,
                                    "ok": True
                                }).encode()
                            ))
                            await ws.send_str(ack_frame)
                            logger.info(f"[tunnel] Client registered tunnel {mapping.tunnel_id} -> {local_host}:{local_port}")

                except ProtocolError as e:
                    logger.error(f"[x] Protocol error: {e}")
                    continue
                    
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"[x] WebSocket error: {ws.exception()}")
                break
                
    except asyncio.CancelledError:
        logger.info(f"[*] Connection cancelled for {remote_addr}")
    except Exception as e:
        logger.error(f"[x] Handler error: {e}")
    finally:
        # Detach from session
        if current_session:
            session_manager.detach(current_session, current_role, ws)
        
        logger.info(f"[-] Connection closed: {remote_addr}")
    
    return ws


async def file_websocket_handler(request):
    """Dedicated WebSocket handler for file operations (Phase 6)."""
    ws = web.WebSocketResponse(heartbeat=30, compress=True)
    can_prepare = ws.can_prepare(request)
    if not can_prepare.ok:
        return web.Response(text="revpty file ws", content_type="text/plain")
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.Response(status=401, text="unauthorized")
    await ws.prepare(request)
    remote_addr = request.remote or "unknown"
    logger.info(f"[+] File WS from {remote_addr}")

    current_session = None
    current_role = None

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    frame = decode(msg.data)
                    current_session = frame.session
                    current_role = frame.role

                    # Handle attach/detach for file channel
                    if frame.type == FrameType.ATTACH.value:
                        session = await session_manager.attach(
                            frame.session, frame.role, ws
                        )
                        logger.info(f"[+] FILE {frame.role.upper()} attached to '{frame.session}'")
                        continue

                    if frame.type == FrameType.DETACH.value:
                        session_manager.detach(frame.session, frame.role, ws)
                        continue

                    # Only accept FILE frames on this endpoint
                    if frame.type != FrameType.FILE.value:
                        continue

                    session = session_manager.get(frame.session)
                    if not session:
                        continue

                    session.last_active = time.time()

                    # Security: viewer read-only enforcement
                    if frame.role == Role.VIEWER.value:
                        try:
                            file_op = json.loads(frame.data)
                            if file_op.get("op") not in ("list", "read"):
                                continue
                        except:
                            continue

                    # Route FILE frames to client peers
                    peers = session.get_peer(frame.role)
                    if peers:
                        for peer in list(peers):
                            if not peer.closed:
                                await peer.send_str(msg.data)

                except ProtocolError as e:
                    logger.error(f"[x] File WS protocol error: {e}")
                    continue

            elif msg.type == WSMsgType.ERROR:
                break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[x] File WS error: {e}")
    finally:
        if current_session:
            session_manager.detach(current_session, current_role, ws)
        logger.info(f"[-] File WS closed: {remote_addr}")

    return ws


async def on_startup(app):
    """Start background tasks"""
    global session_manager
    
    cache_size = app.get("cache_size", 131072)
    
    # Initialize Session Manager
    config = SessionConfig(
        shell="/bin/bash",
        idle_timeout=3600,
        enable_log=True,
        output_cache_size=cache_size
    )
    session_manager = SessionManager(pty_factory, config)
    await session_manager.start_cleanup_task(interval=60)
    
    # Start status printer
    app['status_task'] = asyncio.create_task(print_status())


async def on_cleanup(app):
    """Cleanup background tasks"""
    global session_manager
    
    if 'status_task' in app:
        app['status_task'].cancel()
        try:
            await app['status_task']
        except asyncio.CancelledError:
            pass
    
    if session_manager:
        await session_manager.shutdown()


def run(host="0.0.0.0", port=8765, secret=None, cache_size=131072):
    version = __version__
    logger.info(f"[*] revpty server v{version} - Session-based Architecture")
    logger.info(f"[*] Listening on {host}:{port}")
    logger.info(f"[*] Protocol v1 with validation")
    logger.info(f"[*] Session lifecycle > Connection lifecycle")
    logger.info(f"[*] Output cache size: {cache_size} bytes")
    logger.info(f"[*] Ready for connections")
    
    app = web.Application()
    if secret:
        app[SECRET_KEY] = secret
    app["cache_size"] = cache_size
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # Routes with /revpty prefix
    app.router.add_get('/revpty/ws', websocket_handler)
    app.router.add_get('/revpty/gui', gui_handler)
    app.router.add_get('/revpty/ws/file', file_websocket_handler)
    app.router.add_get('/revpty/api/sessions', sessions_api_handler)
    app.router.add_post('/revpty/api/shares', create_share_handler)
    app.router.add_get('/revpty/share/{share_id}', resolve_share_handler)
    app.router.add_route('*', '/revpty/api/tunnels', tunnel_api_handler)
    app.router.add_delete('/revpty/api/tunnels/{tunnel_id}', tunnel_delete_handler)
    app.router.add_static('/revpty/static', STATIC_DIR)
    # Clear tunnel mode
    app.router.add_get('/clear-tunnel', tunnel_clear_handler)
    # Root redirect to GUI
    app.router.add_get('/', lambda r: web.HTTPFound('/revpty/gui'))
    # Catch-all for tunnel (must be last)
    app.router.add_route('*', '/{path:.*}', unified_handler)
    
    web.run_app(app, host=host, port=port, print=lambda s: None)
