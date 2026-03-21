"""
Server-side Tunnel Manager (Phase 7)

Manages HTTP tunnel requests between browser/GUI and client agent.
Tunnels are multiplexed through the existing WebSocket protocol using
a TUNNEL frame type. Each tunnel request gets a unique request_id.
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from revpty.protocol.frame import Frame, FrameType
from revpty.protocol.codec import encode

logger = logging.getLogger(__name__)


@dataclass
class TunnelMapping:
    """An HTTP tunnel mapping: /tunnel/{tunnel_id}/... -> client local_host:local_port"""
    id: str               # Same as tunnel_id
    tunnel_id: str        # User-friendly ID used in the URL path
    session_id: str
    local_host: str       # Target host on the client side
    local_port: int       # Target port on the client side (service port)
    created_at: float = field(default_factory=time.time)


@dataclass
class PendingRequest:
    """A pending tunnel HTTP request waiting for client response."""
    request_id: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    timeout: float = 30.0


class TunnelManager:
    """
    Manages HTTP tunnel mappings and proxies requests through WebSocket.

    Flow:
    1. Browser/API creates a mapping: tunnel_id -> local_host:local_port
    2. HTTP request arrives on server at /tunnel/{tunnel_id}/...
    3. Server serializes the request into a CONTROL frame and sends to client
    4. Client's TunnelProxy forwards the request to local_host:local_port
    5. Client sends response back as a CONTROL frame
    6. Server deserializes and returns the HTTP response
    """

    def __init__(self):
        self.mappings: dict[str, TunnelMapping] = {}  # tunnel_id -> mapping
        self.pending: dict[str, PendingRequest] = {}  # request_id -> pending
        self._queue_window = 5.0  # seconds to queue during disconnect

    def add_mapping(self, session_id: str, local_port: int,
                    local_host: str = "127.0.0.1") -> TunnelMapping:
        """Register a new HTTP tunnel mapping with auto-generated tunnel_id."""
        tunnel_id = uuid.uuid4().hex[:8]
        mapping = TunnelMapping(
            id=tunnel_id,
            tunnel_id=tunnel_id,
            session_id=session_id,
            local_host=local_host,
            local_port=local_port,
        )
        self.mappings[tunnel_id] = mapping
        logger.info(f"[tunnel] Added mapping {tunnel_id} -> {local_host}:{local_port}")
        return mapping

    def remove_mapping(self, tunnel_id: str):
        """Remove a tunnel mapping."""
        self.mappings.pop(tunnel_id, None)
        logger.info(f"[tunnel] Removed mapping {tunnel_id}")

    def get_mapping_by_tunnel_id(self, tunnel_id: str):
        """Look up a mapping by tunnel_id."""
        return self.mappings.get(tunnel_id)

    def list_mappings(self, session_id: str = None) -> list[TunnelMapping]:
        """List all mappings, optionally filtered by session."""
        if session_id:
            return [m for m in self.mappings.values() if m.session_id == session_id]
        return list(self.mappings.values())

    async def proxy_request(self, session, request_id: str, method: str,
                            path: str, headers: dict, body: bytes,
                            mapping: TunnelMapping) -> dict:
        """
        Send an HTTP request through the tunnel to the client.

        Returns dict with: status, headers, body (base64)
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[request_id] = PendingRequest(
            request_id=request_id,
            future=future,
        )

        # Build tunnel request payload
        payload = json.dumps({
            "op": "tunnel_request",
            "request_id": request_id,
            "method": method,
            "path": path,
            "headers": headers,
            "body_b64": body.hex() if body else "",
            "local_host": mapping.local_host,
            "local_port": mapping.local_port,
        }).encode("utf-8")

        # Send to client via session
        tunnel_frame = encode(Frame(
            session=mapping.session_id,
            role="server",
            type=FrameType.CONTROL.value,
            data=payload,
        ))

        sent = False
        for client_ws in list(session.clients):
            if not client_ws.closed:
                try:
                    await client_ws.send_str(tunnel_frame)
                    sent = True
                    break
                except Exception:
                    continue

        if not sent:
            # N7: Queue for brief disconnect window
            self.pending.pop(request_id, None)
            future.cancel()
            return {
                "status": 502,
                "headers": {},
                "body": b"No connected client for tunnel",
            }

        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            self.pending.pop(request_id, None)
            return {
                "status": 504,
                "headers": {},
                "body": b"Tunnel request timed out",
            }

    def handle_response(self, request_id: str, response: dict):
        """Handle a tunnel response from the client."""
        pending = self.pending.pop(request_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(response)

    def cleanup_expired(self):
        """Remove expired pending requests."""
        now = time.time()
        expired = [
            rid for rid, p in self.pending.items()
            if now - p.created_at > p.timeout
        ]
        for rid in expired:
            p = self.pending.pop(rid, None)
            if p and not p.future.done():
                p.future.set_result({
                    "status": 504,
                    "headers": {},
                    "body": b"Tunnel request expired",
                })
