"""
Client-side Tunnel Proxy (Phase 7)

Handles tunnel requests forwarded from the server.
Makes local HTTP connections to the target host:port and
returns responses through the WebSocket.

N7 features:
- Local connection pool reuse
- Request queuing during brief disconnects (5s)
- Large response framing
"""
import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)


class TunnelProxy:
    """
    Forwards tunneled HTTP requests to local services.

    Receives tunnel_request payloads via the CONTROL frame type,
    makes the actual HTTP request to local_host:local_port,
    and sends back the response.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession = None

    async def _ensure_session(self):
        """Reuse a single aiohttp session for connection pooling (N7)."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,
                keepalive_timeout=30,
            )
            self._session = aiohttp.ClientSession(connector=connector)

    async def handle_request(self, payload: dict) -> bytes:
        """
        Process a tunnel_request and return a tunnel_response payload.

        payload keys:
        - request_id: str
        - method: str (GET, POST, etc.)
        - path: str (the request path)
        - headers: dict
        - body_b64: str (hex-encoded body)
        - local_host: str
        - local_port: int
        """
        request_id = payload.get("request_id", "")
        method = payload.get("method", "GET")
        path = payload.get("path", "/")
        headers = payload.get("headers", {})
        body_hex = payload.get("body_b64", "")
        local_host = payload.get("local_host", "127.0.0.1")
        local_port = payload.get("local_port", 8080)

        body = bytes.fromhex(body_hex) if body_hex else None

        url = f"http://{local_host}:{local_port}{path}"

        try:
            await self._ensure_session()

            # Remove hop-by-hop headers
            filtered_headers = {
                k: v for k, v in headers.items()
                if k.lower() not in (
                    "host", "connection", "transfer-encoding",
                    "upgrade", "proxy-connection", "keep-alive"
                )
            }
            filtered_headers["Host"] = f"{local_host}:{local_port}"

            timeout = aiohttp.ClientTimeout(total=25)
            async with self._session.request(
                method, url,
                headers=filtered_headers,
                data=body,
                timeout=timeout,
                allow_redirects=False,
            ) as resp:
                resp_body = await resp.read()
                resp_headers = dict(resp.headers)

                return json.dumps({
                    "op": "tunnel_response",
                    "request_id": request_id,
                    "status": resp.status,
                    "headers": resp_headers,
                    "body_b64": resp_body.hex(),
                }).encode("utf-8")

        except aiohttp.ClientError as e:
            logger.error(f"[tunnel] Request to {url} failed: {e}")
            return json.dumps({
                "op": "tunnel_response",
                "request_id": request_id,
                "status": 502,
                "headers": {},
                "body_b64": f"Tunnel proxy error: {e}".encode().hex(),
            }).encode("utf-8")
        except asyncio.TimeoutError:
            return json.dumps({
                "op": "tunnel_response",
                "request_id": request_id,
                "status": 504,
                "headers": {},
                "body_b64": b"Tunnel proxy timeout".hex(),
            }).encode("utf-8")
        except Exception as e:
            logger.error(f"[tunnel] Unexpected error: {e}")
            return json.dumps({
                "op": "tunnel_response",
                "request_id": request_id,
                "status": 500,
                "headers": {},
                "body_b64": f"Internal proxy error: {e}".encode().hex(),
            }).encode("utf-8")

    async def close(self):
        """Close the connection pool."""
        if self._session and not self._session.closed:
            await self._session.close()
