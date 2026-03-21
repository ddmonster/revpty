import asyncio
import unittest

from revpty.client.agent import Agent
from revpty.client.mux import ConnectionMux, ConnectionMetrics
from revpty.protocol.codec import encode, decode
from revpty.protocol.frame import Frame, FrameType


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

    def start(self):
        pass

    def stop(self):
        self.running = False


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_registers_session_with_mux(self):
        """Agent should register its main session with the mux."""
        agent = Agent("ws://localhost", "s1")
        agent._queue = agent.mux.register("s1")
        self.assertIn("s1", agent.mux._sessions)
        agent.mux.unregister("s1")

    async def test_ws_to_pty_reads_from_queue(self):
        """Agent._ws_to_pty should read frames from its mux queue."""
        agent = Agent("ws://localhost", "s1")
        agent._queue = asyncio.Queue()
        shell = DummyShell(stop_event=agent._stop_event)
        agent.shell_instance = shell

        # Put an INPUT frame into the queue
        input_frame = Frame(session="s1", role="browser", type=FrameType.INPUT.value, data=b"ls")
        await agent._queue.put(input_frame)

        # Run _ws_to_pty briefly then stop
        async def stop_after():
            await asyncio.sleep(0.3)
            agent.running = False
            agent._stop_event.set()

        await asyncio.gather(
            agent._ws_to_pty(),
            stop_after(),
        )
        self.assertEqual(shell.written, [b"ls"])

    async def test_pty_to_ws_sends_via_mux(self):
        """Agent._pty_to_ws should encode OUTPUT and call mux.send."""
        agent = Agent("ws://localhost", "s1")
        sent_frames = []

        # Mock mux.send
        async def mock_send(frame_str, frame_type=""):
            sent_frames.append((frame_str, frame_type))

        agent.mux.send = mock_send
        agent.mux._connected = True  # pretend connected

        shell = DummyShell(reads=[b"hello", b""], stop_event=agent._stop_event)
        agent.shell_instance = shell

        await agent._pty_to_ws()

        # Should have sent at least one OUTPUT frame
        self.assertTrue(len(sent_frames) >= 1)
        frame_str, ftype = sent_frames[0]
        decoded = decode(frame_str)
        self.assertEqual(decoded.type, FrameType.OUTPUT.value)
        self.assertEqual(decoded.data, b"hello")


class ConnectionMuxTests(unittest.IsolatedAsyncioTestCase):
    def test_register_unregister(self):
        mux = ConnectionMux("ws://localhost")
        q = mux.register("s1")
        self.assertIn("s1", mux._sessions)
        self.assertIsInstance(q, asyncio.Queue)
        mux.unregister("s1")
        self.assertNotIn("s1", mux._sessions)

    def test_multiple_sessions(self):
        mux = ConnectionMux("ws://localhost")
        q1 = mux.register("s1")
        q2 = mux.register("s2")
        self.assertIn("s1", mux._sessions)
        self.assertIn("s2", mux._sessions)
        self.assertIsNot(q1, q2)
        mux.unregister("s1")
        mux.unregister("s2")

    def test_metrics_initial(self):
        mux = ConnectionMux("ws://localhost")
        self.assertEqual(mux.metrics.rtt_ms, 0.0)
        self.assertEqual(mux.metrics.bytes_sent, 0)
        self.assertEqual(mux.metrics.reconnect_count, 0)

    def test_metrics_rtt_recording(self):
        m = ConnectionMetrics()
        m.record_rtt(50.0)
        m.record_rtt(100.0)
        self.assertAlmostEqual(m.rtt_ms, 75.0)

    async def test_send_buffers_output_when_disconnected(self):
        """When mux is disconnected, OUTPUT frames should be buffered (N2)."""
        mux = ConnectionMux("ws://localhost")
        mux.register("s1")
        mux._connected = False

        frame = encode(Frame(session="s1", role="client", type=FrameType.OUTPUT.value, data=b"buffered"))
        await mux.send(frame, FrameType.OUTPUT.value)

        self.assertTrue(len(mux._offline_buffers["s1"]) > 0)
        mux.unregister("s1")

    def test_connected_property_false_without_ws(self):
        mux = ConnectionMux("ws://localhost")
        self.assertFalse(mux.connected)
