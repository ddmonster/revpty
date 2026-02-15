import os
import json
import base64
import logging

logger = logging.getLogger(__name__)

class FileManager:
    """Handles file system operations for the agent"""
    
    def handle_message(self, payload: bytes) -> bytes:
        """Process file operation request and return response"""
        try:
            req = json.loads(payload.decode("utf-8"))
            op = req.get("op")
            path = req.get("path", ".")
            
            # Basic security check: resolve path and ensure it exists (for read/list)
            # For now, we allow full access as it's a "revpty" tool.
            full_path = os.path.abspath(os.path.expanduser(path))
            
            if op == "list":
                return self._list_dir(full_path, req.get("id"))
            elif op == "read":
                return self._read_file(full_path, req.get("id"))
            elif op == "write":
                content = req.get("content")
                return self._write_file(full_path, content, req.get("id"))
            else:
                return self._error("Unknown operation", req.get("id"))
                
        except Exception as e:
            logger.error(f"File manager error: {e}")
            return self._error(str(e), req.get("id") if 'req' in locals() else None)

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
            
            # Sort: directories first, then files
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
                
            # Read as binary, encode as base64
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
