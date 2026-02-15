import unittest

from aiohttp import WSMsgType

from revpty.client.agent import Agent
from revpty.protocol.codec import encode
from revpty.protocol.frame import Frame, FrameType


class FakeMessage:
    def __init__(self, msg_type, data=None):
        self.type = msg_type
        self.data = data


class FakeWS:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send_str(self, data):
        self.sent.append(data)

    def exception(self):
        return None


class DummyShell:
    def __init__(self, reads=None, stop_event=None):
        self.reads = list(reads or [])
        self.written = []
        self.running = True
        self.stop_event = stop_event

    async def read(self, size=1024, timeout=None):
        if not self.reads:
            return b""
        data = self.reads.pop(0)
        if not data:
            self.running = False
            if self.stop_event:
                self.stop_event.set()
        return data

    def write(self, data):
        self.written.append(data)

    def resize(self, rows, cols):
        pass

    def is_running(self):
        return self.running


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_to_pty_writes_input(self):
        frame = Frame(session="s1", role="browser", type=FrameType.INPUT.value, data=b"ls")
        ws = FakeWS([FakeMessage(WSMsgType.TEXT, encode(frame))])
        agent = Agent("ws://localhost", "s1")
        agent.connected = True
        shell = DummyShell(stop_event=agent._stop_event)
        agent.shell_instance = shell
        await agent._ws_to_pty(ws)
        self.assertEqual(shell.written, [b"ls"])

    async def test_pty_to_ws_stops_on_empty(self):
        ws = FakeWS([])
        agent = Agent("ws://localhost", "s1")
        agent.connected = True
        shell = DummyShell(reads=[b""], stop_event=agent._stop_event)
        agent.shell_instance = shell
        await agent._pty_to_ws(ws)
        self.assertFalse(agent.connected)
        self.assertEqual(ws.sent, [])
