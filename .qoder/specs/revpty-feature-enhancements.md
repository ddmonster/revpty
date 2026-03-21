# revpty Feature Enhancements Plan

## Context

revpty v0.5.5 is a WebSocket-based reverse PTY shell tool. Several features already exist (Web GUI, readonly mode, auto-reconnect, --install), but need enhancement. New features required: WebSocket multiplexing, large file transfer, port mapping/tunneling, share links, and server-side output caching.

**Already implemented (no work needed):** Web GUI (#3), Readonly mode (#9)

---

## Cross-cutting: Harsh Network Resilience

The following optimizations apply across multiple phases and must be considered during implementation of each feature. The goal is reliable operation on high-latency, packet-loss, bandwidth-constrained, and intermittently-disconnecting networks.

### N1. WebSocket Compression (Phase 4 - mux.py)

**Current problem:** `compress=False` hardcoded in `agent.py` line 478. Terminal output is highly compressible text — compression can reduce bandwidth 60-80%.

**Change:** Enable per-message deflate compression on both client and server:
- `revpty/client/mux.py`: `ws_connect(..., compress=15)` (deflate window size)
- `revpty/server/app.py`: `WebSocketResponse(heartbeat=30, compress=True)` in both `websocket_handler` and `file_websocket_handler`
- GUI JS: Browser WebSocket API uses `permessage-deflate` by default, no change needed

### N2. Client-side Output Buffering During Disconnect (Phase 4 - mux.py)

**Current problem:** When WS disconnects, PTY output is lost. On reconnect, new output appears but the gap is permanent.

**Change in `ConnectionMux`:**
- Add `_offline_buffer: dict[str, bytearray]` — per-session buffer for outbound OUTPUT frames while disconnected
- When `send()` is called but WS is not connected: buffer the frame data (up to 256KB per session)
- On successful reconnect + re-ATTACH: flush buffered frames in order before resuming normal operation
- If buffer overflows: drop oldest data (same ring buffer strategy as server cache)

### N3. Faster Dead Connection Detection (Phase 4 - mux.py)

**Current problem:** `heartbeat=30` means up to 60s to detect a dead connection (30s interval + 30s pong timeout). Too slow for interactive use.

**Change:**
- Application-level heartbeat: reduce from 30s to **10s** interval in `ConnectionMux._heartbeat()`
- Track pong responses: if 2 consecutive pongs are missed, proactively close and reconnect (don't wait for aiohttp's built-in timeout)
- `aiohttp.ClientTimeout`: reduce `sock_read` from 30s to 15s
- WS-level heartbeat: keep `heartbeat=30` as safety net (aiohttp level), but our app-level 10s heartbeat catches problems faster

### N4. Exponential Backoff with Jitter + Immediate First Retry (Phase 1/4)

**Current problem:** After disconnect, first retry waits `retry_delay` seconds. On a brief network blip, this wastes time.

**Change in reconnect logic (both `mux.py` and `attach.py`):**
- **Immediate first retry**: attempt #1 after disconnect has 0s delay (network blip recovery)
- Then exponential backoff: 1s → 2s → 4s → 8s → 10s (cap)
- Jitter: `random.uniform(0, delay * 0.3)` instead of fixed `0-0.5s` — scales with delay to better distribute retries
- Track `last_connected_duration`: if previous connection lasted >60s, reset retry_delay to 0 (was a stable connection, likely a transient issue)

### N5. Frame Priority Queue (Phase 4 - mux.py)

**Current problem:** All frames share a single send path. A large file_chunk can block a terminal keystroke.

**Change in `ConnectionMux.send()`:**
- Replace single send lock with a **priority queue** (2 levels):
  - **High priority**: INPUT, OUTPUT, RESIZE, PING, PONG, ATTACH, DETACH, STATUS, CONTROL
  - **Low priority**: FILE, TUNNEL
- `send(frame_str, priority="high"|"low")`: callers specify priority
- Internal `_send_loop()` drains high-priority queue first, then low-priority
- This ensures terminal I/O is never blocked by bulk transfers

### N6. Chunked File Transfer Resilience (Phase 5)

**Current plan enhancement:**
- **Sliding window** instead of stop-and-wait: sender can have up to `window_size` (default 4) unacknowledged chunks in flight. This dramatically improves throughput on high-latency connections.
- **Per-chunk checksum**: each `file_chunk` includes a CRC32 of the chunk data. Receiver verifies before ACK. On mismatch, sends `file_chunk_nack` to request retransmit.
- **Adaptive chunk size**: start with 64KB chunks. If RTT > 500ms or packet loss detected, halve chunk size to 32KB. If RTT < 100ms with no loss, double up to 128KB.
- **Chunk timeout**: if no `file_chunk_ack` received within `2 * measured_RTT + 5s`, retransmit the chunk (up to 3 retries before aborting transfer)
- **Transfer state persistence**: `ChunkedFileTransfer` state (transfer_id, completed_seqs) survives reconnect, enabling true resume after connection drop

### N7. Tunnel Request Resilience (Phase 7)

**Enhancements for `TunnelManager`/`TunnelProxy`:**
- **Request timeout**: 30s default, but configurable. If WS disconnects during a pending tunnel request, immediately return 502 Bad Gateway
- **Request queue**: if WS is temporarily disconnected (reconnecting), queue tunnel requests for up to 5s before returning 503. This covers brief network blips transparently.
- **Response streaming**: for large HTTP responses (>256KB), stream as multiple TUNNEL response frames rather than one huge frame. Prevents blocking the mux.
- **Connection pooling** in `TunnelProxy`: reuse `aiohttp.ClientSession` for local HTTP requests to `localhost:{port}` instead of creating per-request

### N8. Server-side Reconnect Continuity (Phase 2/4)

**Current problem:** When client reconnects, server creates a fresh session attach. If old WS is still in the session's `clients` set (not yet cleaned up), there can be duplicate routing.

**Changes:**
- **Session.attach()**: when a client attaches, check for and remove any closed/stale WebSockets from `clients` set before adding the new one
- **Stale WS detection**: periodically (every heartbeat cycle) scan session connection sets and remove closed WebSockets
- **Output cache + reconnect synergy**: server-side output cache (Phase 2) naturally handles the browser reconnect case. For client reconnect, the mux's offline buffer (N2) handles the gap.

### N9. Connection Quality Metrics (Phase 4 - mux.py)

Track connection health in `ConnectionMux` for adaptive behavior:
```python
@dataclass
class ConnectionMetrics:
    rtt_ms: float = 0          # measured from ping/pong
    rtt_samples: list = ...    # last 10 RTT measurements
    pong_miss_count: int = 0   # consecutive missed pongs
    bytes_sent: int = 0
    bytes_received: int = 0
    reconnect_count: int = 0
    last_connected_at: float = 0
    last_disconnected_at: float = 0
```

- RTT measured from application-level ping/pong timestamps
- Used by: adaptive chunk size (N6), dead connection detection (N3), tunnel timeout (N7)
- Exposed via `mux.metrics` for logging and debugging

---

## Phase 1: Quick Wins (no dependencies)

### 1.1 Auto-reconnect max delay: 30s -> 10s + immediate first retry (see N4)

**Files:**
- `revpty/client/agent.py` line 225: `min(retry_delay * 2, 30)` -> `min(retry_delay * 2, 10)`
- `revpty/client/agent.py` line 543: `min(retry_delay * 2, 30)` -> `min(retry_delay * 2, 10)`
- `revpty/cli/attach.py` line 288: `min(retry_delay * 2, 30)` -> `min(retry_delay * 2, 10)`
- All three locations: add immediate first retry (0s delay) after disconnect, scale jitter to `random.uniform(0, delay * 0.3)`
- Track `last_connected_duration` to reset delay for stable-then-dropped connections

### 1.2 ws= parameter removal from GUI URL

**File:** `revpty/server/app.py` GUI_HTML JavaScript

After `const qs = new URLSearchParams(location.search)` (~line 181), add:
```js
if (qs.has("ws")) {
  qs.delete("ws")
  history.replaceState(null, '', location.pathname + '?' + qs.toString())
}
```

### 1.3 --install user-level systemd support

**File:** `revpty/cli/main.py`

- Add `--user` argument to both `server()` and `client()` argparse
- Modify `_install_systemd(service_name, exec_args, user_mode=False)`:
  - When `user_mode=True`: skip root check, install to `~/.config/systemd/user/`, use `systemctl --user`, set `WantedBy=default.target`
  - When `user_mode=False`: keep existing behavior unchanged

---

## Phase 2: Server-side Output Caching

### 2.1 Output Ring Buffer

**New file:** `revpty/session/buffer.py`

```python
class OutputRingBuffer:
    def __init__(self, capacity=131072):  # 128KB default
        self._buf = bytearray()
        self.capacity = capacity
    
    def append(self, data: bytes): ...  # append + truncate front if over capacity
    def get_all(self) -> bytes: ...     # return current buffer content
    def clear(self): ...
```

### 2.2 Session integration

**File:** `revpty/session/manager.py`
- `SessionConfig`: add `output_cache_size: int = 131072`
- `Session.__init__`: add `self.output_buffer = OutputRingBuffer(config.output_cache_size)`
- `Session.close()`: clear buffer

**File:** `revpty/session/__init__.py` - export OutputRingBuffer (optional)

### 2.3 Server caching + replay (incorporates N8)

**File:** `revpty/server/app.py` `websocket_handler`

- **Cache write**: When routing OUTPUT frames from client, append `frame.data` to `session.output_buffer`
- **Cache replay**: When handling ATTACH from browser/viewer, if `session.output_buffer` is non-empty, send a synthetic OUTPUT frame with the cached content to the newly attached WS
- **Stale WS cleanup (N8)**: on ATTACH, scan `session.clients`/`session.browsers` sets and remove any closed WebSockets before adding the new one. This prevents duplicate routing on rapid reconnect.
- **WS compression (N1)**: change `WebSocketResponse(heartbeat=30)` to `WebSocketResponse(heartbeat=30, compress=True)` in both `websocket_handler` and `file_websocket_handler`

### 2.4 CLI parameter

**File:** `revpty/cli/main.py` `server()`: add `--cache-size` argument, pass to `SessionConfig`

---

## Phase 3: Share Functionality

### 3.1 Server-side share store

**File:** `revpty/server/app.py`

Add `ShareRecord` dataclass and global `share_store: dict[str, ShareRecord]`:
```python
@dataclass
class ShareRecord:
    id: str           # 8-digit numeric
    session_id: str
    mode: str         # "ro" or "rw"
    secret: str       # original secret for proxy auth
    created_at: float
```

**New API endpoints:**

1. `POST /api/shares` - Create share link
   - Auth: require secret (same as existing pattern)
   - Body: `{ "session_id": "xxx", "mode": "ro"|"rw" }`
   - Response: `{ "id": "38572946", "url": "/share/38572946" }`
   - Generate 8-digit numeric ID: `random.randint(10000000, 99999999)` with collision retry

2. `GET /share/{share_id}` - Resolve share link
   - Lookup `share_store[share_id]`
   - If found: 302 redirect to `/gui?session=<sid>&seceret=<secret>&mode=<mode>`
   - If not found: return error HTML page

**Register routes** in `run()`:
```python
app.router.add_post('/api/shares', create_share_handler)
app.router.add_get('/share/{share_id}', resolve_share_handler)
```

### 3.2 GUI Share Modal

**File:** `revpty/server/app.py` GUI_HTML

- Add `<button id="btn-share" disabled>Share</button>` to `.bar`
- Add `#share-modal` with: mode selector (radio: Read Only / Full Control), generate button, link display with copy button
- JS: POST to `/api/shares`, display result URL, `navigator.clipboard.writeText()` for copy
- Enable button on connect, disable on disconnect

---

## Phase 4: WebSocket Connection Multiplexing

### 4.1 ConnectionMux class (incorporates N1-N5, N8-N9)

**New file:** `revpty/client/mux.py`

```python
class ConnectionMux:
    """Multiplexes multiple sessions over a single WebSocket with network resilience"""
    def __init__(self, server, proxy, secret): ...
    async def connect(self): ...           # establish single WS with compression (N1) + reconnect loop (N4)
    async def send(self, frame_str, priority="high"): ...  # priority queue send (N5)
    def register(self, session_id) -> asyncio.Queue: ...   # register worker, returns inbound queue
    def unregister(self, session_id): ...
    async def close(self): ...
    
    # Network resilience internals
    _offline_buffers: dict[str, bytearray]  # N2: per-session output buffer during disconnect
    _metrics: ConnectionMetrics              # N9: RTT, loss, throughput tracking
    _send_queue_high: asyncio.Queue          # N5: priority queues
    _send_queue_low: asyncio.Queue           # N5
    _pong_miss_count: int                    # N3: dead connection detection
```

Core logic:
- `_connect_with_retry()`: moved from Agent, immediate first retry + exponential backoff max 10s (N4). Enable `compress=15` on ws_connect (N1)
- `_dispatch_loop()`: reads frames from WS, routes by `frame.session` to registered worker queues. Measures RTT from pong responses (N9).
- `_send_loop()`: drains high-priority queue first, then low-priority (N5). If WS down, buffers OUTPUT frames to offline_buffers (N2).
- `_heartbeat()`: 10s interval, tracks missed pongs, force-reconnect after 2 misses (N3)
- On reconnect: flush offline buffers (N2), auto re-ATTACH all registered sessions, clean stale references (N8)

### 4.2 Agent refactor

**File:** `revpty/client/agent.py`

- `Agent.__init__`: create `ConnectionMux` instead of managing WS directly
- Remove `Agent._connect_with_retry()`, `Agent._heartbeat()` (moved to mux)
- `Agent._ws_to_pty()`: read from `mux.register(self.session)` queue instead of raw WS
- `Agent._pty_to_ws()`: call `mux.send()` instead of `ws.send_str()`
- `Agent._handle_control` new_shell: pass `mux` to ShellWorker instead of server/proxy/secret

### 4.3 ShellWorker refactor

**File:** `revpty/client/agent.py`

- Constructor takes `mux: ConnectionMux` instead of `server/proxy/secret`
- Remove `_connect_with_retry()`, `_heartbeat()`
- `_ws_to_pty()`: read from `mux.register(self.session)` queue
- `_pty_to_ws()`: call `mux.send()`
- `run()`: just start ws_to_pty + pty_to_ws tasks
- `stop()`: call `mux.unregister(self.session)`

**Server-side**: No changes needed. Frames already route by session ID.

---

## Phase 5: Large File Transfer (Chunked)

### 5.1 Protocol extension

**File:** `revpty/protocol/frame.py`

No new FrameType needed. Chunked file operations use existing `FILE` frame with extended `op` values in the data JSON.

New FILE ops:

| op | Direction | Purpose | Key fields |
|----|-----------|---------|------------|
| `file_init` | requester->provider | Start chunked transfer | transfer_id, path, direction("upload"/"download"), chunk_size(65536) |
| `file_init_ack` | provider->requester | Confirm, provide metadata | transfer_id, total_size, total_chunks, resume_seq |
| `file_chunk` | provider->requester (download) or reverse | Data chunk | transfer_id, seq, data(base64) |
| `file_chunk_ack` | receiver | Acknowledge chunk | transfer_id, seq |
| `file_complete` | sender | Transfer done | transfer_id, checksum(sha256) |
| `file_complete_ack` | receiver | Verified | transfer_id, ok |
| `file_abort` | either | Cancel | transfer_id, reason |

### 5.2 Client file manager (incorporates N6)

**File:** `revpty/client/file_manager.py`

Add `ChunkedFileTransfer` class:
- Manages state for one transfer: transfer_id, path, total_size, chunk_size, current_seq, `completed_seqs: set`
- `next_chunk()`: lazy read next chunk from file
- `seek_to(seq)`: for resume support
- `write_chunk(seq, data)`: for upload receiving, verifies CRC32 per chunk (N6)
- **Sliding window**: sender maintains `window_size=4` unacknowledged chunks in flight (N6)
- **Adaptive chunk size**: start 64KB, halve if `mux.metrics.rtt_ms > 500`, double if `rtt_ms < 100` (N6, max 128KB)
- **Chunk timeout + retransmit**: retransmit after `2 * rtt_ms + 5000ms`, max 3 retries (N6)
- **Resume persistence**: `completed_seqs` survives reconnect via transfer_id lookup (N6)

Modify `FileManager`:
- Add `active_transfers: dict[str, ChunkedFileTransfer]`
- `handle_message()`: add handlers for new ops including `file_chunk_nack` (CRC mismatch retransmit)
- Backward compatible: existing `read`/`write` ops still work for small files

### 5.3 GUI chunked transfer

**File:** `revpty/server/app.py` GUI_HTML JavaScript

Add JS classes:
- `ChunkedDownloader`: sends `file_init`, receives chunks, assembles, triggers browser download, shows progress bar
- `ChunkedUploader`: slices File into chunks, sends `file_init` + chunks, shows progress

Modify existing handlers:
- `downloadFile()`: use ChunkedDownloader for large files
- `uploadInput.change`: use ChunkedUploader for large files
- Add progress bar UI to file explorer panel

---

## Phase 6: Dedicated File WebSocket

### 6.1 Server endpoint

**File:** `revpty/server/app.py`

- New handler `file_websocket_handler` at `/ws/file`
- Same auth as `websocket_handler`
- Only accepts FILE type frames, rejects others
- Routes FILE frames to client's main WS via `session.clients`

**File:** `revpty/session/manager.py`
- `Session`: add `file_browsers: Set` for file WS connections
- `attach()`/`detach()`: handle "file_browser" role

### 6.2 GUI dual connection

**File:** `revpty/server/app.py` GUI_HTML JavaScript

- New `wsFile` WebSocket connecting to `/ws/file`
- `sendJson()`: use `wsFile` for file operations
- File response handling moves to `wsFile.onmessage`
- Fallback: if `/ws/file` fails, use main `ws` (backward compatible)

---

## Phase 7: Port Mapping / HTTP Tunneling

### 7.1 Protocol extension

**File:** `revpty/protocol/frame.py`
- New FrameType: `TUNNEL = "tunnel"`
- Frame.validate(): add TUNNEL rules (requires data)

**File:** `revpty/protocol/codec.py`
- No changes needed (TUNNEL frames use existing data field)

### 7.2 Tunnel manager (server) (incorporates N7)

**New file:** `revpty/server/tunnel.py`

```python
@dataclass
class TunnelInfo:
    tunnel_id: str       # 8-digit numeric
    session_id: str
    local_port: int
    created_at: float

class TunnelManager:
    tunnels: dict[str, TunnelInfo]
    pending_requests: dict[str, asyncio.Future]
    _request_queue: dict[str, list]  # N7: queued requests during brief disconnects
    
    def register(self, session_id, local_port) -> TunnelInfo: ...
    def unregister(self, tunnel_id): ...
    async def forward_request(self, tunnel_id, method, path, headers, body) -> TunnelResponse: ...
```

**HTTP handler**: `tunnel_http_handler(request)` at route `/tunnel/{tunnel_id}/{path:.*}`
- Serialize HTTP request into TUNNEL frame
- Send through client's WS
- If WS temporarily disconnected: queue request up to 5s before returning 503 (N7)
- Wait on `asyncio.Future` with 30s timeout for response
- If WS drops during pending request: return 502 immediately (N7)
- **Response streaming**: large responses (>256KB) arrive as multiple TUNNEL frames, reassembled before sending HTTP response (N7)
- Return HTTP response to caller

### 7.3 Tunnel proxy (client) (incorporates N7)

**New file:** `revpty/client/tunnel_proxy.py`

```python
class TunnelProxy:
    _session: aiohttp.ClientSession  # N7: reuse connection pool to localhost
    
    async def handle_tunnel_frame(self, frame_data: dict) -> bytes:
        """Proxy HTTP request to localhost:port, return response as TUNNEL frame"""
    
    async def close(self):
        """Cleanup connection pool"""
```

Uses persistent `aiohttp.ClientSession` (N7: connection pooling) to forward to `localhost:{port}`.
Large response bodies (>256KB) are split into multiple TUNNEL response frames (N7).

### 7.4 Integration

**File:** `revpty/server/app.py`
- `on_startup`: create global `TunnelManager`
- `websocket_handler`: handle CONTROL ops `tunnel_register`/`tunnel_unregister`, handle TUNNEL response frames
- `run()`: add route `/tunnel/{tunnel_id}/{path:.*}`

**File:** `revpty/client/agent.py`
- `_ws_to_pty()`: handle TUNNEL frames, delegate to `TunnelProxy`
- `_handle_control()`: handle `tunnel_register`/`tunnel_unregister` ops

### 7.5 GUI port mapping panel

**File:** `revpty/server/app.py` GUI_HTML
- Add `<button id="btn-ports" disabled>Ports</button>` to `.bar`
- Add `#port-panel` sidebar: list active tunnels, register new port form, copy tunnel URL
- JS: send/receive CONTROL frames for `register_port`/`list_ports`/`unregister_port`

---

## New Files Summary

| File | Phase | Purpose |
|------|-------|---------|
| `revpty/session/buffer.py` | 2 | Output ring buffer |
| `revpty/client/mux.py` | 4 | WebSocket connection multiplexer with network resilience (N1-N5, N8-N9) |
| `revpty/server/tunnel.py` | 7 | Tunnel manager + HTTP handler with request queuing (N7) |
| `revpty/client/tunnel_proxy.py` | 7 | Local HTTP proxy with connection pooling (N7) |

## Modified Files Summary

| File | Phases | Changes |
|------|--------|---------|
| `revpty/server/app.py` | 1,2,3,5,6,7 | GUI JS fixes, output cache replay, share API, file WS endpoint, tunnel routes, GUI modal/panel additions |
| `revpty/client/agent.py` | 1,4,5,7 | Retry delay, mux refactor, tunnel frame handling |
| `revpty/cli/main.py` | 1 | --user flag, --cache-size |
| `revpty/cli/attach.py` | 1 | Retry delay |
| `revpty/session/manager.py` | 2,6 | output_buffer, file_browsers, SessionConfig extension |
| `revpty/session/__init__.py` | 2 | Export new classes |
| `revpty/client/file_manager.py` | 5 | ChunkedFileTransfer, new op handlers |
| `revpty/protocol/frame.py` | 7 | TUNNEL FrameType |
| `revpty/protocol/codec.py` | 7 | TUNNEL validation (minimal) |

---

## Implementation Order

```
Phase 1 (Quick Wins) ─── no dependencies
  ├── 1.1 retry delay 30->10
  ├── 1.2 ws= param removal
  └── 1.3 --install --user

Phase 2 (Output Cache) ─── no dependencies
  ├── 2.1 OutputRingBuffer
  ├── 2.2 Session integration
  └── 2.3 Server cache/replay

Phase 3 (Share) ─── no dependencies
  ├── 3.1 ShareStore + API
  └── 3.2 GUI Share modal

Phase 4 (WS Mux) ─── no dependencies
  ├── 4.1 ConnectionMux
  ├── 4.2 Agent refactor
  └── 4.3 ShellWorker refactor

Phase 5 (Chunked Files) ─── depends on Phase 4 (mux)
  ├── 5.1 Protocol ops
  ├── 5.2 FileManager enhancement
  └── 5.3 GUI chunked UI

Phase 6 (File WS) ─── depends on Phase 5
  ├── 6.1 Server /ws/file endpoint
  └── 6.2 GUI dual WS

Phase 7 (Port Mapping) ─── depends on Phase 4 (mux)
  ├── 7.1 Protocol TUNNEL type
  ├── 7.2 TunnelManager
  ├── 7.3 TunnelProxy
  ├── 7.4 Integration
  └── 7.5 GUI port panel
```

---

## Verification

### Per-phase testing:

1. **Phase 1**: Run `revpty-client` with intentional server disconnect, verify reconnect within 10s max. Verify immediate first retry (0s delay) on first disconnect. Test `--install --user` creates service in `~/.config/systemd/user/`. Load GUI with `?ws=xxx` param, verify URL is cleaned.

2. **Phase 2**: Start server+client+browser. Disconnect browser, reconnect - verify terminal history is replayed. Check buffer doesn't exceed configured size. Test with multiple rapid reconnects.

3. **Phase 3**: Click Share in GUI, generate link with read-only and read-write modes. Visit share link, verify redirect and correct mode.

4. **Phase 4**: Create multiple shells from GUI. Verify only 1 WebSocket connection exists (check server logs - should show single connection from client IP). All shells should function normally. Verify WS compression is active (check frame sizes in network tab). Kill server, restart — verify client auto-reconnects and all shells resume without user action.

5. **Phase 5**: Upload/download files >1MB via GUI. Verify progress display. Disconnect during transfer, reconnect, verify resume works from last acknowledged chunk. Test adaptive chunk sizing by simulating high latency.

6. **Phase 6**: Transfer large file while typing in terminal simultaneously. Terminal should remain responsive. Check browser network tab shows two WS connections.

7. **Phase 7**: Register port 8000 from GUI. Start `python -m http.server 8000` on client. Access `/tunnel/<id>/` from browser. Verify HTTP content is proxied correctly. Test tunnel request queuing: disconnect client briefly (<5s), verify in-flight HTTP request still completes.

### Network resilience testing:

Use `tc` (Linux traffic control) or `Network Link Conditioner` (macOS) to simulate:

| Scenario | Settings | Verify |
|----------|----------|--------|
| High latency | 500ms RTT | Terminal remains usable, file chunks resize to 32KB |
| Packet loss | 10% loss | Terminal works (slight lag), file transfer completes with retransmits |
| Bandwidth limit | 100 Kbps | Compression (N1) keeps terminal responsive, file transfer slow but progresses |
| Intermittent drops | 5s blackout every 30s | Auto-reconnect recovers within 1-2s, no output lost (N2), file transfer resumes (N6) |
| Complete disconnect | Kill network 60s | Reconnect on restore, offline buffer replays, share links still resolve |

### Existing tests:
- Run `python -m pytest tests/` after each phase to verify no regressions
- Add new test files: `tests/test_output_buffer.py`, `tests/test_mux.py`, `tests/test_chunked_transfer.py`, `tests/test_tunnel.py`
