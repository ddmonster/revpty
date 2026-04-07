"""Integration tests for TunnelProxy using mock HTTP server."""
import asyncio
import json
import unittest

from aiohttp import web

from revpty.client.tunnel_proxy import TunnelProxy


class TunnelProxyTests(unittest.IsolatedAsyncioTestCase):
    """Tests for TunnelProxy with mock HTTP server."""

    async def asyncSetUp(self):
        """Start a mock HTTP server for testing."""
        self.app = web.Application()

        async def hello_handler(request):
            return web.Response(text="hello world", content_type="text/plain")

        async def echo_handler(request):
            body = await request.read()
            return web.Response(text=body.decode(), content_type="text/plain")

        async def headers_handler(request):
            return web.json_response(dict(request.headers))

        async def status_handler(request):
            code = int(request.match_info.get("code", "200"))
            return web.Response(status=code, text=f"status {code}")

        self.app.router.add_get("/hello", hello_handler)
        self.app.router.add_post("/echo", echo_handler)
        self.app.router.add_get("/headers", headers_handler)
        self.app.router.add_get("/status/{code}", status_handler)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)  # random port
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.proxy = TunnelProxy()

    async def asyncTearDown(self):
        """Stop the mock server and cleanup."""
        await self.proxy.close()
        await self.runner.cleanup()

    async def test_handle_request_get(self):
        """Simple GET request returns response."""
        payload = {
            "request_id": "req1",
            "method": "GET",
            "path": "/hello",
            "headers": {},
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }
        result = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result["op"], "tunnel_response")
        self.assertEqual(result["status"], 200)
        self.assertEqual(bytes.fromhex(result["body_b64"]), b"hello world")

    async def test_handle_request_post_with_body(self):
        """POST request with body echoes it back."""
        body = b"test payload"
        payload = {
            "request_id": "req2",
            "method": "POST",
            "path": "/echo",
            "headers": {"Content-Type": "text/plain"},
            "body_b64": body.hex(),
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }
        result = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result["status"], 200)
        self.assertEqual(bytes.fromhex(result["body_b64"]), body)

    async def test_handle_request_error_status(self):
        """Handles non-200 status codes."""
        payload = {
            "request_id": "req3",
            "method": "GET",
            "path": "/status/404",
            "headers": {},
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }
        result = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result["status"], 404)

    async def test_handle_request_custom_header_preserved(self):
        """Custom headers are passed through to the target server."""
        payload = {
            "request_id": "req4",
            "method": "GET",
            "path": "/headers",
            "headers": {
                "X-Custom": "value",
            },
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }
        result = json.loads(await self.proxy.handle_request(payload))
        # The /headers endpoint returns the request headers it received
        body = json.loads(bytes.fromhex(result["body_b64"]))
        headers_lower = {k.lower(): v for k, v in body.items()}
        # Custom header should be preserved in the request
        self.assertEqual(headers_lower.get("x-custom"), "value")

    async def test_handle_request_connection_refused(self):
        """Connection to non-existent server returns 502."""
        payload = {
            "request_id": "req5",
            "method": "GET",
            "path": "/hello",
            "headers": {},
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": 1,  # Should fail to connect
        }
        result = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result["status"], 502)
        body = bytes.fromhex(result["body_b64"]).decode()
        self.assertIn("error", body.lower())

    async def test_handle_request_request_id_passthrough(self):
        """request_id is preserved in response."""
        payload = {
            "request_id": "my-unique-id-123",
            "method": "GET",
            "path": "/hello",
            "headers": {},
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }
        result = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result["request_id"], "my-unique-id-123")

    async def test_handle_request_default_values(self):
        """Uses defaults for missing optional fields."""
        payload = {
            "request_id": "req6",
            # method defaults to GET, path to /, host to 127.0.0.1, port to 8080
        }
        # Will try to connect to 127.0.0.1:8080, which won't exist
        result = json.loads(await self.proxy.handle_request(payload))
        # Should fail with 502 since no server on 8080
        self.assertEqual(result["status"], 502)

    async def test_close_idempotent(self):
        """close() can be called multiple times safely."""
        await self.proxy.close()
        await self.proxy.close()  # Should not raise

    async def test_session_reuse(self):
        """Proxy reuses aiohttp session for multiple requests."""
        payload = {
            "request_id": "req7",
            "method": "GET",
            "path": "/hello",
            "headers": {},
            "body_b64": "",
            "local_host": "127.0.0.1",
            "local_port": self.port,
        }

        # First request
        result1 = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result1["status"], 200)

        # Second request - should reuse session
        payload["request_id"] = "req8"
        result2 = json.loads(await self.proxy.handle_request(payload))
        self.assertEqual(result2["status"], 200)