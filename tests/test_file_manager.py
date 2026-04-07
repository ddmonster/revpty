import base64
import json
import os
import tempfile
import unittest
import zlib

from revpty.client.file_manager import FileManager, ChunkedFileTransfer


class ChunkedTransferProtocolTests(unittest.TestCase):
    """Tests for chunked transfer protocol handlers."""

    def test_file_chunk_valid_write(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            fm = FileManager()
            # Init upload
            req = json.dumps({"op": "file_init", "path": path, "direction": "upload", "chunk_size": 50}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            # Write valid chunk
            data = b"hello world"
            crc = zlib.crc32(data) & 0xFFFFFFFF
            req2 = json.dumps({
                "op": "file_chunk", "transfer_id": tid, "seq": 0,
                "data": base64.b64encode(data).decode(), "crc32": crc,
            }).encode()
            resp2 = json.loads(fm.handle_message(req2))
            self.assertEqual(resp2["op"], "file_chunk_ack")
            self.assertEqual(resp2["seq"], 0)
        finally:
            os.unlink(path)

    def test_file_chunk_crc_mismatch(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "upload"}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            data = b"test"
            req2 = json.dumps({
                "op": "file_chunk", "transfer_id": tid, "seq": 0,
                "data": base64.b64encode(data).decode(), "crc32": 999999,
            }).encode()
            resp2 = json.loads(fm.handle_message(req2))
            self.assertEqual(resp2["op"], "file_chunk_nack")
            self.assertEqual(resp2["reason"], "crc_mismatch")
        finally:
            os.unlink(path)

    def test_file_chunk_unknown_transfer(self):
        fm = FileManager()
        req = json.dumps({
            "op": "file_chunk", "transfer_id": "nonexistent", "seq": 0,
            "data": base64.b64encode(b"x").decode(), "crc32": 0,
        }).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_file_chunk_ack_sliding_window(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"A" * 200)
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "download", "chunk_size": 50}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            # Initial read sends window_size chunks
            req2 = json.dumps({"op": "file_chunk_ack", "transfer_id": tid, "seq": 0}).encode()
            resp2 = json.loads(fm.handle_message(req2))
            # Should return next chunks in window
            if "chunks" in resp2:
                ops = [c["op"] for c in resp2["chunks"]]
            else:
                ops = [resp2["op"]]
            self.assertTrue(all(op == "file_chunk" for op in ops))
        finally:
            os.unlink(path)

    def test_file_chunk_ack_unknown_transfer(self):
        fm = FileManager()
        req = json.dumps({"op": "file_chunk_ack", "transfer_id": "nonexistent", "seq": 0}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "noop")

    def test_file_chunk_nack_retransmit(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"A" * 100)
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "download", "chunk_size": 50}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            # Request retransmit of seq 0
            req2 = json.dumps({"op": "file_chunk_nack", "transfer_id": tid, "seq": 0}).encode()
            resp2 = json.loads(fm.handle_message(req2))
            self.assertEqual(resp2["op"], "file_chunk")
            self.assertEqual(resp2["seq"], 0)
        finally:
            os.unlink(path)

    def test_file_chunk_nack_unknown_transfer(self):
        fm = FileManager()
        req = json.dumps({"op": "file_chunk_nack", "transfer_id": "nonexistent", "seq": 0}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "noop")

    def test_file_complete_cleanup(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "upload"}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            req2 = json.dumps({"op": "file_complete", "transfer_id": tid}).encode()
            resp2 = json.loads(fm.handle_message(req2))
            self.assertEqual(resp2["op"], "file_complete_ack")
            self.assertNotIn(tid, fm.active_transfers)
        finally:
            os.unlink(path)

    def test_file_complete_unknown_transfer(self):
        fm = FileManager()
        req = json.dumps({"op": "file_complete", "transfer_id": "nonexistent"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_file_complete_ack_cleanup(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "file_init", "path": path, "direction": "upload"}).encode()
            resp = json.loads(fm.handle_message(req))
            tid = resp["transfer_id"]

            req2 = json.dumps({"op": "file_complete_ack", "transfer_id": tid}).encode()
            resp2 = json.loads(fm.handle_message(req2))
            self.assertEqual(resp2["op"], "noop")
            self.assertNotIn(tid, fm.active_transfers)
        finally:
            os.unlink(path)


class SimpleFileOpsTests(unittest.TestCase):
    """Tests for _list_dir, _read_file, _write_file."""

    def test_list_dir_valid(self):
        fm = FileManager()
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "a.txt"), "w") as f:
                f.write("hello")
            os.makedirs(os.path.join(tmpdir, "subdir"))
            req = json.dumps({"op": "list", "path": tmpdir, "id": "r1"}).encode()
            resp = json.loads(fm.handle_message(req))
            self.assertEqual(resp["op"], "list_ack")
            names = [e["name"] for e in resp["entries"]]
            self.assertIn("a.txt", names)
            self.assertIn("subdir", names)
            # Directories sort first
            self.assertEqual(resp["entries"][0]["name"], "subdir")
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_list_dir_nonexistent(self):
        fm = FileManager()
        req = json.dumps({"op": "list", "path": "/nonexistent/path", "id": "r1"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_read_file_valid(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"file content")
            path = f.name
        try:
            fm = FileManager()
            req = json.dumps({"op": "read", "path": path, "id": "r1"}).encode()
            resp = json.loads(fm.handle_message(req))
            self.assertEqual(resp["op"], "read_ack")
            decoded = base64.b64decode(resp["content"])
            self.assertEqual(decoded, b"file content")
        finally:
            os.unlink(path)

    def test_read_file_nonexistent(self):
        fm = FileManager()
        req = json.dumps({"op": "read", "path": "/nonexistent/file", "id": "r1"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_read_file_is_directory(self):
        tmpdir = tempfile.mkdtemp()
        try:
            fm = FileManager()
            req = json.dumps({"op": "read", "path": tmpdir, "id": "r1"}).encode()
            resp = json.loads(fm.handle_message(req))
            self.assertEqual(resp["op"], "error")
        finally:
            os.rmdir(tmpdir)

    def test_write_file_valid(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            fm = FileManager()
            content_b64 = base64.b64encode(b"new content").decode()
            req = json.dumps({"op": "write", "path": path, "content": content_b64, "id": "r1"}).encode()
            resp = json.loads(fm.handle_message(req))
            self.assertEqual(resp["op"], "write_ack")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"new content")
        finally:
            os.unlink(path)

    def test_handle_message_unknown_op(self):
        fm = FileManager()
        req = json.dumps({"op": "nonexistent_op", "path": ".", "id": "r1"}).encode()
        resp = json.loads(fm.handle_message(req))
        self.assertEqual(resp["op"], "error")

    def test_handle_message_malformed_json(self):
        fm = FileManager()
        resp = json.loads(fm.handle_message(b"not json"))
        self.assertEqual(resp["op"], "error")
