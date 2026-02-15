import io
import sys
import unittest

from aiohttp import WSMsgType

from revpty.cli.attach import InteractiveTerminal
from revpty.protocol.codec import encode, decode
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


class DummyStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, value):
        return len(value)

    def flush(self):
        pass


class AttachTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_from_ws_handles_output_and_ping(self):
        output_frame = Frame(session="s1", role="client", type=FrameType.OUTPUT.value, data=b"ok")
        ping_frame = Frame(session="s1", role="client", type=FrameType.PING.value)
        status_frame = Frame(session="s1", role="browser", type=FrameType.STATUS.value, data=b'{"peers":0}')
        ws = FakeWS([
            FakeMessage(WSMsgType.TEXT, encode(output_frame)),
            FakeMessage(WSMsgType.TEXT, encode(ping_frame)),
            FakeMessage(WSMsgType.TEXT, encode(status_frame)),
        ])
        term = InteractiveTerminal(ws, "s1", "testattach")

        original_stdout = sys.stdout
        sys.stdout = DummyStdout()
        try:
            await term._read_from_ws()
            self.assertEqual(sys.stdout.buffer.getvalue(), b"ok")
            self.assertTrue(term._status_event.is_set())
        finally:
            sys.stdout = original_stdout

        self.assertTrue(ws.sent)
        sent_frames = [decode(item) for item in ws.sent]
        self.assertTrue(any(f.type == FrameType.PONG.value for f in sent_frames))
