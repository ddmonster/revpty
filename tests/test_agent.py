import asyncio
import json
import time
import unittest

from revpty.client.agent import Agent
from revpty.client.mux import ConnectionMux, ConnectionMetrics, _frame_priority, PRIORITY_HIGH, PRIORITY_LOW
from revpty.protocol.codec import encode, decode
from revpty.protocol.frame import Frame, FrameType


class FramePriorityTests(unittest.TestCase):
    def test_file_is_low_priority(self):
        self.assertEqual(_frame_priority(FrameType.FILE.value), PRIORITY_LOW)

    def test_input_is_high_priority(self):
        self.assertEqual(_frame_priority(FrameType.INPUT.value), PRIORITY_HIGH)

    def test_output_is_high_priority(self):
        self.assertEqual(_frame_priority(FrameType.OUTPUT.value), PRIORITY_HIGH)

    def test_resize_is_high_priority(self):
        self.assertEqual(_frame_priority(FrameType.RESIZE.value), PRIORITY_HIGH)

    def test_ping_pong_high_priority(self):
        self.assertEqual(_frame_priority(FrameType.PING.value), PRIORITY_HIGH)
        self.assertEqual(_frame_priority(FrameType.PONG.value), PRIORITY_HIGH)

    def test_control_is_high_priority(self):
        self.assertEqual(_frame_priority(FrameType.CONTROL.value), PRIORITY_HIGH)


class ConnectionMetricsTests(unittest.TestCase):
    def test_record_rtt_averages_samples(self):
        m = ConnectionMetrics()
        m.record_rtt(50.0)
        m.record_rtt(100.0)
        self.assertAlmostEqual(m.rtt_ms, 75.0)

    def test_record_rtt_keeps_last_10_samples(self):
        m = ConnectionMetrics()
        for i in range(15):
            m.record_rtt(float(i))
        self.assertEqual(len(m.rtt_samples), 10)
        # Should have last 10: 5-14
        self.assertAlmostEqual(m.rtt_samples[0], 5.0)
        self.assertAlmostEqual(m.rtt_samples[-1], 14.0)


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

    async def test_register_sends_attach_when_connected(self):
        """When already connected, register should send ATTACH frame."""
        mux = ConnectionMux("ws://localhost")
        mux._connected = True

        sent = []
        class FakeWS:
            closed = False
            async def send_str(self, msg):
                sent.append(msg)

        mux._ws = FakeWS()
        mux.register("s1")

        # Wait for the create_task to complete
        await asyncio.sleep(0.01)

        self.assertEqual(len(sent), 1)
        frame = decode(sent[0])
        self.assertEqual(frame.type, FrameType.ATTACH.value)
        self.assertEqual(frame.session, "s1")

    async def test_send_routes_to_high_priority_queue(self):
        """INPUT frames go to high priority queue when connected."""
        mux = ConnectionMux("ws://localhost")
        mux._connected = True
        mux._ws = type("FakeWS", (), {"closed": False})()

        frame = encode(Frame(session="s1", role="client", type=FrameType.INPUT.value, data=b"x"))
        await mux.send(frame, FrameType.INPUT.value)

        self.assertEqual(mux._send_queue_high.qsize(), 1)
        self.assertEqual(mux._send_queue_low.qsize(), 0)

    async def test_send_routes_to_low_priority_queue(self):
        """FILE frames go to low priority queue when connected."""
        mux = ConnectionMux("ws://localhost")
        mux._connected = True
        mux._ws = type("FakeWS", (), {"closed": False})()

        frame = encode(Frame(session="s1", role="client", type=FrameType.FILE.value, data=b"{}"))
        await mux.send(frame, FrameType.FILE.value)

        self.assertEqual(mux._send_queue_low.qsize(), 1)
        self.assertEqual(mux._send_queue_high.qsize(), 0)

    async def test_send_non_output_dropped_when_disconnected(self):
        """Non-OUTPUT frames are silently dropped when disconnected (no buffering)."""
        mux = ConnectionMux("ws://localhost")
        mux._connected = False
        mux.register("s1")

        frame = encode(Frame(session="s1", role="client", type=FrameType.RESIZE.value, rows=24, cols=80))
        await mux.send(frame, FrameType.RESIZE.value)

        # Should not buffer
        self.assertEqual(len(mux._offline_buffers["s1"]), 0)

    async def test_reattach_all_sends_attach_frames(self):
        """_reattach_all should send ATTACH for all registered sessions."""
        mux = ConnectionMux("ws://localhost")

        sent = []
        class FakeWS:
            closed = False
            async def send_str(self, msg):
                sent.append(msg)

        mux._ws = FakeWS()
        mux._session_roles = {"s1": "client", "s2": "browser"}

        await mux._reattach_all()

        self.assertEqual(len(sent), 2)
        sessions = {decode(s).session for s in sent}
        self.assertEqual(sessions, {"s1", "s2"})

    async def test_flush_offline_buffers_sends_output(self):
        """_flush_offline_buffers should send buffered OUTPUT and clear buffer."""
        mux = ConnectionMux("ws://localhost")

        sent = []
        class FakeWS:
            closed = False
            async def send_str(self, msg):
                sent.append(msg)

        mux._ws = FakeWS()
        mux._session_roles = {"s1": "client"}
        mux._offline_buffers["s1"] = bytearray(b"cached")

        await mux._flush_offline_buffers()

        self.assertEqual(len(sent), 1)
        frame = decode(sent[0])
        self.assertEqual(frame.type, FrameType.OUTPUT.value)
        self.assertEqual(frame.data, b"cached")
        self.assertEqual(len(mux._offline_buffers["s1"]), 0)

    async def test_close_cancels_tasks(self):
        """close() should cancel all running tasks."""
        mux = ConnectionMux("ws://localhost")

        class FakeWS:
            closed = False
            async def close(self):
                self.closed = True

        mux._ws = FakeWS()
        mux._connected = True

        # Create dummy tasks
        async def dummy():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        mux._heartbeat_task = asyncio.create_task(dummy())
        mux._send_task = asyncio.create_task(dummy())
        mux._dispatch_task = asyncio.create_task(dummy())

        await mux.close()

        self.assertTrue(mux._heartbeat_task.done())
        self.assertTrue(mux._send_task.done())
        self.assertTrue(mux._dispatch_task.done())
        self.assertFalse(mux._connected)
