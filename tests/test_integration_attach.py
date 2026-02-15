import asyncio
import unittest

from aiohttp import web, ClientSession, WSMsgType, WSServerHandshakeError

from revpty.server.app import websocket_handler, on_startup, on_cleanup, SECRET_KEY
from revpty.protocol.codec import encode, decode
from revpty.protocol.frame import Frame, FrameType


class IntegrationAttachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = web.Application()
        self.app.on_startup.append(on_startup)
        self.app.on_cleanup.append(on_cleanup)
        self.app.router.add_get("/", websocket_handler)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets
        self.port = sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_attach_receives_status(self):
        url = f"ws://127.0.0.1:{self.port}/"
        async with ClientSession() as session:
            async with session.ws_connect(url) as ws:
                await ws.send_str(encode(Frame(
                    session="s1",
                    role="browser",
                    type=FrameType.ATTACH.value,
                )))
                msg = await ws.receive(timeout=2)
                self.assertEqual(msg.type, WSMsgType.TEXT)
                frame = decode(msg.data)
                self.assertEqual(frame.type, FrameType.STATUS.value)

    async def test_attach_requires_secret(self):
        app = web.Application()
        app[SECRET_KEY] = "s3"
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        app.router.add_get("/", websocket_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/"
        try:
            async with ClientSession() as session:
                with self.assertRaises(WSServerHandshakeError):
                    await session.ws_connect(url)
                async with session.ws_connect(url, headers={"X-Revpty-Secret": "s3"}) as ws:
                    await ws.send_str(encode(Frame(
                        session="s1",
                        role="browser",
                        type=FrameType.ATTACH.value,
                    )))
                    msg = await ws.receive(timeout=2)
                    self.assertEqual(msg.type, WSMsgType.TEXT)
                    frame = decode(msg.data)
                    self.assertEqual(frame.type, FrameType.STATUS.value)
        finally:
            await runner.cleanup()
