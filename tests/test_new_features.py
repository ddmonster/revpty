"""Tests for new features: Phase 2-7"""
import asyncio
import json
import os
import tempfile
import time
import unittest
import zlib

from revpty.session.buffer import OutputRingBuffer
from revpty.session.manager import Session, SessionConfig, SessionState
from revpty.client.file_manager import FileManager, ChunkedFileTransfer
from revpty.server.tunnel import TunnelManager


class OutputRingBufferTests(unittest.TestCase):
    def test_append_and_get_all(self):
        buf = OutputRingBuffer(capacity=100)
        buf.append(b"hello")
        buf.append(b" world")
        self.assertEqual(buf.get_all(), b"hello world")

    def test_overflow_drops_oldest(self):
        buf = OutputRingBuffer(capacity=10)
        buf.append(b"12345")
        buf.append(b"67890")
        buf.append(b"abc")
        # Total 13 bytes, capacity 10 => drops first 3 => "4567890abc"
        result = buf.get_all()
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith(b"abc"))

    def test_clear(self):
        buf = OutputRingBuffer(capacity=100)
        buf.append(b"data")
        buf.clear()
        self.assertEqual(len(buf), 0)
        self.assertFalse(buf)

    def test_bool(self):
        buf = OutputRingBuffer(capacity=100)
        self.assertFalse(buf)
        buf.append(b"x")
        self.assertTrue(buf)


class SessionOutputBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_has_output_buffer(self):
        config = SessionConfig(output_cache_size=256)
        session = Session("test", config)
        self.assertIsInstance(session.output_buffer, OutputRingBuffer)
        self.assertEqual(session.output_buffer.capacity, 256)

    async def test_session_close_clears_buffer(self):
        config = SessionConfig()
        session = Session("test", config)
        session.output_buffer.append(b"cached data")
        await session.close()
        self.assertEqual(len(session.output_buffer), 0)

    async def test_stale_ws_cleanup_on_attach(self):
        config = SessionConfig()
        session = Session("test", config)

        class FakeWS:
            def __init__(self, closed=False):
                self.closed = closed

        stale = FakeWS(closed=True)
        active = FakeWS(closed=False)
        session.clients.add(stale)
        session.attach("client", active)
        self.assertNotIn(stale, session.clients)
        self.assertIn(active, session.clients)


class ChunkedFileTransferTests(unittest.TestCase):
    def test_read_chunk_crc32(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"A" * 100)
            path = f.name
        try:
            xfer = ChunkedFileTransfer("t1", path, "download", chunk_size=50)
            xfer.open_for_read()
            self.assertEqual(xfer.total_chunks, 2)

            data, crc = xfer.read_chunk(0)
            self.assertEqual(len(data), 50)
            expected_crc = zlib.crc32(data) & 0xFFFFFFFF
            self.assertEqual(crc, expected_crc)
            xfer.close()
        finally:
            os.unlink(path)

    def test_write_chunk_crc_verify(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            xfer = ChunkedFileTransfer("t2", path, "upload", chunk_size=50)
            xfer.open_for_write()

            data = b"hello world"
            crc = zlib.crc32(data) & 0xFFFFFFFF
            self.assertTrue(xfer.write_chunk(0, data, crc))

            # Wrong CRC should fail
            self.assertFalse(xfer.write_chunk(1, data, crc + 1))

            xfer.close()
        finally:
            os.unlink(path)

    def test_adaptive_chunk_size(self):
        xfer = ChunkedFileTransfer("t3", "/tmp/x", "download", chunk_size=65536)
        xfer.adapt_chunk_size(600)  # High RTT => smaller
        self.assertLess(xfer.chunk_size, 65536)

        xfer2 = ChunkedFileTransfer("t4", "/tmp/x", "download", chunk_size=32768)
        xfer2.adapt_chunk_size(50)  # Low RTT => bigger
        self.assertGreater(xfer2.chunk_size, 32768)


class FileManagerChunkedTests(unittest.TestCase):
    def test_file_init_download(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"X" * 200)
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "download"}).encode()
            resp = json.loads(fm.handle_message(req))
            self.assertEqual(resp["op"], "file_init_ack")
            self.assertEqual(resp["total_size"], 200)
            self.assertGreater(resp["total_chunks"], 0)

            # Cleanup
            xfer = fm.active_transfers.get(resp["transfer_id"])
            if xfer:
                xfer.close()
        finally:
            os.unlink(path)

    def test_file_init_nonexistent(self):
        fm = FileManager()
        req = json.dumps({"op": "file_init", "path": "/nonexistent/file", "direction": "download"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_file_abort(self):
        fm = FileManager()
        # Create a fake transfer
        xfer = ChunkedFileTransfer("abort_test", "/tmp/x", "download")
        fm.active_transfers["abort_test"] = xfer
        req = json.dumps({"op": "file_abort", "transfer_id": "abort_test"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "file_abort_ack")
        self.assertNotIn("abort_test", fm.active_transfers)


class TunnelManagerTests(unittest.TestCase):
    def test_add_and_get_mapping(self):
        tm = TunnelManager()
        m = tm.add_mapping("s1", 3000, "127.0.0.1")
        self.assertIsNotNone(m.tunnel_id)
        self.assertEqual(m.local_port, 3000)
        self.assertEqual(m.local_host, "127.0.0.1")
        self.assertEqual(m.session_id, "s1")

        found = tm.get_mapping_by_tunnel_id(m.tunnel_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.local_port, 3000)

    def test_remove_mapping(self):
        tm = TunnelManager()
        m = tm.add_mapping("s1", 8080)
        tm.remove_mapping(m.tunnel_id)
        self.assertIsNone(tm.get_mapping_by_tunnel_id(m.tunnel_id))

    def test_list_mappings_filter(self):
        tm = TunnelManager()
        tm.add_mapping("s1", 8080)
        tm.add_mapping("s1", 9090)
        tm.add_mapping("s2", 8080)

        s1_mappings = tm.list_mappings("s1")
        self.assertEqual(len(s1_mappings), 2)

        all_mappings = tm.list_mappings()
        self.assertEqual(len(all_mappings), 3)

    def test_handle_response(self):
        tm = TunnelManager()
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        from revpty.server.tunnel import PendingRequest
        tm.pending["req1"] = PendingRequest(request_id="req1", future=future)

        result = {"status": 200, "headers": {}, "body": b"ok"}
        tm.handle_response("req1", result)

        self.assertTrue(future.done())
        self.assertEqual(future.result()["status"], 200)
        loop.close()

    def test_cleanup_expired(self):
        tm = TunnelManager()
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        from revpty.server.tunnel import PendingRequest
        tm.pending["old"] = PendingRequest(
            request_id="old", future=future,
            created_at=time.time() - 60, timeout=30
        )

        tm.cleanup_expired()
        self.assertNotIn("old", tm.pending)
        self.assertTrue(future.done())
        loop.close()
