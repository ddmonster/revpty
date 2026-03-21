import os
import json
import base64
import hashlib
import logging
import uuid
import zlib

logger = logging.getLogger(__name__)


class ChunkedFileTransfer:
    """Manages state for a single chunked file transfer (N6)."""

    def __init__(self, transfer_id: str, path: str, direction: str,
                 chunk_size: int = 65536, total_size: int = 0):
        self.transfer_id = transfer_id
        self.path = path
        self.direction = direction  # "upload" or "download"
        self.chunk_size = chunk_size
        self.total_size = total_size
        self.total_chunks = 0
        self.completed_seqs: set = set()
        self.current_seq = 0
        self._file = None
        self._sha256 = hashlib.sha256()

        # Sliding window (N6)
        self.window_size = 4
        self.in_flight: set = set()

        # Adaptive chunk size (N6)
        self.min_chunk = 32768   # 32KB
        self.max_chunk = 131072  # 128KB

    def open_for_read(self):
        self.total_size = os.path.getsize(self.path)
        self.total_chunks = (self.total_size + self.chunk_size - 1) // self.chunk_size
        self._file = open(self.path, "rb")

    def open_for_write(self):
        self._file = open(self.path, "wb")

    def read_chunk(self, seq: int) -> tuple[bytes, int]:
        """Read chunk at sequence number, return (data, crc32)."""
        offset = seq * self.chunk_size
        self._file.seek(offset)
        data = self._file.read(self.chunk_size)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        return data, crc

    def write_chunk(self, seq: int, data: bytes, expected_crc: int) -> bool:
        """Write chunk, verify CRC32. Returns True if OK."""
        actual_crc = zlib.crc32(data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return False
        offset = seq * self.chunk_size
        self._file.seek(offset)
        self._file.write(data)
        self._sha256.update(data)
        self.completed_seqs.add(seq)
        return True

    def checksum(self) -> str:
        return self._sha256.hexdigest()

    def adapt_chunk_size(self, rtt_ms: float):
        """Adaptive chunk sizing based on RTT (N6)."""
        if rtt_ms > 500 and self.chunk_size > self.min_chunk:
            self.chunk_size = max(self.chunk_size // 2, self.min_chunk)
        elif rtt_ms < 100 and self.chunk_size < self.max_chunk:
            self.chunk_size = min(self.chunk_size * 2, self.max_chunk)

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


class FileManager:
    """Handles file system operations for the agent"""

    def __init__(self):
        self.active_transfers: dict[str, ChunkedFileTransfer] = {}

    def handle_message(self, payload: bytes) -> bytes:
        """Process file operation request and return response"""
        try:
            req = json.loads(payload.decode("utf-8"))
            op = req.get("op")
            path = req.get("path", ".")

            full_path = os.path.abspath(os.path.expanduser(path))

            if op == "list":
                return self._list_dir(full_path, req.get("id"))
            elif op == "read":
                return self._read_file(full_path, req.get("id"))
            elif op == "write":
                content = req.get("content")
                return self._write_file(full_path, content, req.get("id"))
            # Chunked transfer ops
            elif op == "file_init":
                return self._handle_file_init(req)
            elif op == "file_chunk":
                return self._handle_file_chunk(req)
            elif op == "file_chunk_ack":
                return self._handle_file_chunk_ack(req)
            elif op == "file_chunk_nack":
                return self._handle_file_chunk_nack(req)
            elif op == "file_complete":
                return self._handle_file_complete(req)
            elif op == "file_complete_ack":
                return self._handle_file_complete_ack(req)
            elif op == "file_abort":
                return self._handle_file_abort(req)
            else:
                return self._error("Unknown operation", req.get("id"))

        except Exception as e:
            logger.error(f"File manager error: {e}")
            return self._error(str(e), req.get("id") if 'req' in locals() else None)

    # --- Chunked transfer handlers (Phase 5) ---

    def _handle_file_init(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id") or uuid.uuid4().hex[:16]
        path = os.path.abspath(os.path.expanduser(req.get("path", "")))
        direction = req.get("direction", "download")
        chunk_size = req.get("chunk_size", 65536)

        try:
            xfer = ChunkedFileTransfer(transfer_id, path, direction, chunk_size)
            if direction == "download":
                if not os.path.isfile(path):
                    return self._error(f"Not a file: {path}", transfer_id)
                xfer.open_for_read()
            else:
                xfer.open_for_write()

            self.active_transfers[transfer_id] = xfer

            return json.dumps({
                "op": "file_init_ack",
                "transfer_id": transfer_id,
                "total_size": xfer.total_size,
                "total_chunks": xfer.total_chunks,
                "chunk_size": xfer.chunk_size,
                "resume_seq": 0,
            }).encode("utf-8")
        except Exception as e:
            return self._error(f"file_init error: {e}", transfer_id)

    def _handle_file_chunk(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        xfer = self.active_transfers.get(transfer_id)
        if not xfer:
            return self._error(f"Unknown transfer: {transfer_id}", transfer_id)

        seq = req.get("seq", 0)
        data = base64.b64decode(req.get("data", ""))
        expected_crc = req.get("crc32", 0)

        ok = xfer.write_chunk(seq, data, expected_crc)
        if ok:
            return json.dumps({
                "op": "file_chunk_ack",
                "transfer_id": transfer_id,
                "seq": seq,
            }).encode("utf-8")
        else:
            return json.dumps({
                "op": "file_chunk_nack",
                "transfer_id": transfer_id,
                "seq": seq,
                "reason": "crc_mismatch",
            }).encode("utf-8")

    def _handle_file_chunk_ack(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        seq = req.get("seq", 0)
        xfer = self.active_transfers.get(transfer_id)
        if not xfer:
            return json.dumps({"op": "noop"}).encode("utf-8")

        xfer.completed_seqs.add(seq)
        xfer.in_flight.discard(seq)

        chunks = []
        while len(xfer.in_flight) < xfer.window_size and xfer.current_seq < xfer.total_chunks:
            next_seq = xfer.current_seq
            if next_seq not in xfer.completed_seqs:
                data, crc = xfer.read_chunk(next_seq)
                xfer.in_flight.add(next_seq)
                chunks.append({
                    "op": "file_chunk",
                    "transfer_id": transfer_id,
                    "seq": next_seq,
                    "data": base64.b64encode(data).decode("ascii"),
                    "crc32": crc,
                })
            xfer.current_seq += 1

        if not chunks and not xfer.in_flight:
            return json.dumps({
                "op": "file_complete",
                "transfer_id": transfer_id,
                "checksum": "",
            }).encode("utf-8")

        if len(chunks) == 1:
            return json.dumps(chunks[0]).encode("utf-8")
        elif chunks:
            return json.dumps({"op": "file_chunks_batch", "chunks": chunks}).encode("utf-8")

        return json.dumps({"op": "noop"}).encode("utf-8")

    def _handle_file_chunk_nack(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        seq = req.get("seq", 0)
        xfer = self.active_transfers.get(transfer_id)
        if not xfer:
            return json.dumps({"op": "noop"}).encode("utf-8")

        try:
            data, crc = xfer.read_chunk(seq)
            return json.dumps({
                "op": "file_chunk",
                "transfer_id": transfer_id,
                "seq": seq,
                "data": base64.b64encode(data).decode("ascii"),
                "crc32": crc,
            }).encode("utf-8")
        except Exception as e:
            return self._error(f"Retransmit error: {e}", transfer_id)

    def _handle_file_complete(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        xfer = self.active_transfers.get(transfer_id)
        if not xfer:
            return self._error(f"Unknown transfer: {transfer_id}", transfer_id)

        xfer.close()
        self.active_transfers.pop(transfer_id, None)

        return json.dumps({
            "op": "file_complete_ack",
            "transfer_id": transfer_id,
            "ok": True,
        }).encode("utf-8")

    def _handle_file_complete_ack(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        xfer = self.active_transfers.pop(transfer_id, None)
        if xfer:
            xfer.close()
        return json.dumps({"op": "noop"}).encode("utf-8")

    def _handle_file_abort(self, req: dict) -> bytes:
        transfer_id = req.get("transfer_id")
        xfer = self.active_transfers.pop(transfer_id, None)
        if xfer:
            xfer.close()
        return json.dumps({
            "op": "file_abort_ack",
            "transfer_id": transfer_id,
        }).encode("utf-8")

    # --- Original simple file ops ---

    def _list_dir(self, path: str, req_id: str) -> bytes:
        try:
            if not os.path.exists(path):
                return self._error(f"Path not found: {path}", req_id)

            entries = []
            with os.scandir(path) as it:
                for entry in it:
                    stat = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    })

            entries.sort(key=lambda x: (not x["is_dir"], x["name"]))

            return json.dumps({
                "op": "list_ack",
                "id": req_id,
                "path": path,
                "entries": entries
            }).encode("utf-8")

        except Exception as e:
            return self._error(f"List error: {e}", req_id)

    def _read_file(self, path: str, req_id: str) -> bytes:
        try:
            if not os.path.isfile(path):
                return self._error(f"Not a file: {path}", req_id)

            with open(path, "rb") as f:
                content = f.read()

            return json.dumps({
                "op": "read_ack",
                "id": req_id,
                "path": path,
                "content": base64.b64encode(content).decode("ascii")
            }).encode("utf-8")

        except Exception as e:
            return self._error(f"Read error: {e}", req_id)

    def _write_file(self, path: str, content_b64: str, req_id: str) -> bytes:
        try:
            content = base64.b64decode(content_b64)
            with open(path, "wb") as f:
                f.write(content)

            return json.dumps({
                "op": "write_ack",
                "id": req_id,
                "path": path
            }).encode("utf-8")

        except Exception as e:
            return self._error(f"Write error: {e}", req_id)

    def _error(self, msg: str, req_id: str) -> bytes:
        return json.dumps({
            "op": "error",
            "id": req_id,
            "error": msg
        }).encode("utf-8")
