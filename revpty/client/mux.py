"""
WebSocket Connection Multiplexer (Phase 4)

Multiplexes multiple sessions over a single WebSocket connection.
Incorporates network resilience features N1-N5, N8-N9.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import ClientSession, WSMsgType

from revpty.protocol.frame import Frame, FrameType
from revpty.protocol.codec import encode, decode

logger = logging.getLogger(__name__)

# Frame priority levels
PRIORITY_HIGH = 0  # INPUT, OUTPUT, RESIZE, PING, PONG, ATTACH, DETACH, STATUS, CONTROL
PRIORITY_LOW = 1   # FILE, TUNNEL

# Map frame types to priorities (N5)
_LOW_PRIORITY_TYPES = frozenset({FrameType.FILE.value})

def _frame_priority(frame_type: str) -> int:
    if frame_type in _LOW_PRIORITY_TYPES:
        return PRIORITY_LOW
    return PRIORITY_HIGH


@dataclass
class ConnectionMetrics:
    """Connection quality metrics (N9)"""
    rtt_ms: float = 0.0
    rtt_samples: list = field(default_factory=list)
    pong_miss_count: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    reconnect_count: int = 0
    last_connected_at: float = 0.0
    last_disconnected_at: float = 0.0

    def record_rtt(self, rtt_ms: float):
        self.rtt_samples.append(rtt_ms)
        if len(self.rtt_samples) > 10:
            self.rtt_samples.pop(0)
        self.rtt_ms = sum(self.rtt_samples) / len(self.rtt_samples)


class ConnectionMux:
    """
    Multiplexes multiple sessions over a single WebSocket connection.

    Features:
    - N1: WebSocket per-message deflate compression
    - N2: Client-side output buffering during disconnect
    - N3: Fast dead connection detection (10s heartbeat)
    - N4: Immediate first retry + exponential backoff (max 10s)
    - N5: Frame priority queue (terminal I/O > file/tunnel)
    - N8: Stale WS cleanup on reconnect (server-side)
    - N9: Connection quality metrics (RTT, loss tracking)
    """

    def __init__(self, server: str, proxy: str = None, secret: str = None,
                 cf_client_id: str = None, cf_client_secret: str = None):
        self.server = server
        self.proxy = proxy
        self.secret = secret
        self.cf_client_id = cf_client_id
        self.cf_client_secret = cf_client_secret

        # Connection state
        self._ws = None
        self._connected = False
        self._closing = False
        self._stop_event = asyncio.Event()

        # Session registration: session_id -> asyncio.Queue for inbound frames
        self._sessions: dict[str, asyncio.Queue] = {}
        self._session_roles: dict[str, str] = {}

        # Priority send queues (N5)
        self._send_queue_high: asyncio.Queue = asyncio.Queue()
        self._send_queue_low: asyncio.Queue = asyncio.Queue()

        # Offline output buffers (N2): per-session buffer during disconnect
        self._offline_buffers: dict[str, bytearray] = {}
        self._offline_buffer_max = 262144  # 256KB per session

        # Metrics (N9)
        self.metrics = ConnectionMetrics()

        # Heartbeat tracking (N3)
        self._ping_sent_at: float = 0.0
        self._pong_miss_count: int = 0

        # Tasks
        self._connect_task: asyncio.Task = None
        self._send_task: asyncio.Task = None
        self._dispatch_task: asyncio.Task = None
        self._heartbeat_task: asyncio.Task = None

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    def register(self, session_id: str, role: str = "client") -> asyncio.Queue:
        """Register a session worker. Returns an inbound frame queue."""
        if session_id not in self._sessions:
            self._sessions[session_id] = asyncio.Queue()
            self._session_roles[session_id] = role
            self._offline_buffers[session_id] = bytearray()
            logger.info(f"[mux] Registered session '{session_id}'")
            # Send ATTACH immediately if already connected
            if self._connected and self._ws and not self._ws.closed:
                try:
                    attach_frame = encode(Frame(
                        session=session_id,
                        role=role,
                        type="attach",
                    ))
                    asyncio.create_task(self._ws.send_str(attach_frame))
                    logger.info(f"[mux] Attached session '{session_id}'")
                except Exception as e:
                    logger.error(f"[mux] Failed to attach '{session_id}': {e}")
        return self._sessions[session_id]

    def unregister(self, session_id: str):
        """Unregister a session worker."""
        self._sessions.pop(session_id, None)
        self._session_roles.pop(session_id, None)
        self._offline_buffers.pop(session_id, None)
        logger.info(f"[mux] Unregistered session '{session_id}'")

    async def send(self, frame_str: str, frame_type: str = ""):
        """Queue a frame for sending with priority routing (N5)."""
        priority = _frame_priority(frame_type)
        if not self.connected:
            # Buffer OUTPUT frames during disconnect (N2)
            if frame_type == FrameType.OUTPUT.value:
                try:
                    frame_obj = decode(frame_str)
                    sid = frame_obj.session
                    buf = self._offline_buffers.get(sid)
                    if buf is not None and frame_obj.data:
                        buf.extend(frame_obj.data)
                        if len(buf) > self._offline_buffer_max:
                            overflow = len(buf) - self._offline_buffer_max
                            del buf[:overflow]
                except Exception:
                    pass
            return

        if priority == PRIORITY_HIGH:
            await self._send_queue_high.put(frame_str)
        else:
            await self._send_queue_low.put(frame_str)

    async def start(self):
        """Start the mux connection loop."""
        self._connect_task = asyncio.create_task(self._connect_loop())

    async def close(self):
        """Gracefully shut down the mux."""
        self._closing = True
        self._stop_event.set()

        for task in [self._heartbeat_task, self._send_task, self._dispatch_task, self._connect_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._connected = False
        logger.info("[mux] Closed")

    # ---- Internal: connection loop with retry (N4) ----

    async def _connect_loop(self):
        retry_delay = 0  # immediate first retry (N4)
        last_connected_at = 0

        while not self._stop_event.is_set():
            try:
                headers = {}
                if self.secret:
                    headers["X-Revpty-Secret"] = self.secret
                if self.cf_client_id and self.cf_client_secret:
                    headers["CF-Access-Client-Id"] = self.cf_client_id
                    headers["CF-Access-Client-Secret"] = self.cf_client_secret
                if not headers:
                    headers = None
                timeout = aiohttp.ClientTimeout(
                    total=30, connect=10, sock_connect=10, sock_read=15  # N3: reduced sock_read
                )

                proxy_info = f" via {self.proxy}" if self.proxy else ""
                logger.info(f"[mux] Connecting to {self.server}{proxy_info}...")

                async with ClientSession(timeout=timeout) as http:
                    async with http.ws_connect(
                        self.server,
                        proxy=self.proxy,
                        headers=headers,
                        heartbeat=30,  # aiohttp-level safety net
                        compress=15,   # N1: per-message deflate
                    ) as ws:
                        self._ws = ws
                        self._connected = True
                        self._pong_miss_count = 0
                        self.metrics.last_connected_at = time.time()
                        self.metrics.reconnect_count += 1
                        last_connected_at = time.time()
                        retry_delay = 0

                        logger.info(f"[mux] Connected ({len(self._sessions)} sessions)")

                        # Re-attach all registered sessions
                        await self._reattach_all()
                        # Flush offline buffers (N2)
                        await self._flush_offline_buffers()

                        # Start sub-tasks
                        self._send_task = asyncio.create_task(self._send_loop())
                        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
                        self._heartbeat_task = asyncio.create_task(self._heartbeat(interval=10))  # N3

                        done, pending = await asyncio.wait(
                            [self._send_task, self._dispatch_task, self._heartbeat_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass

                        self._connected = False
                        self.metrics.last_disconnected_at = time.time()

                        if self._stop_event.is_set():
                            break

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self._connected = False
                self.metrics.last_disconnected_at = time.time()
                # N4: reset delay if previous connection was stable (>60s)
                if last_connected_at > 0 and (time.time() - last_connected_at) > 60:
                    retry_delay = 0
                    last_connected_at = 0
                logger.warning(f"[mux] Connection failed: {type(e).__name__}: {e}")

                if retry_delay > 0:
                    logger.info(f"[mux] Reconnecting in {retry_delay:.1f}s...")
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                    except asyncio.TimeoutError:
                        pass
                    except asyncio.CancelledError:
                        break
                else:
                    logger.info("[mux] Reconnecting immediately...")

                if retry_delay == 0:
                    retry_delay = 1
                else:
                    retry_delay = min(retry_delay * 2, 10) + random.uniform(0, retry_delay * 0.3)
            except Exception as e:
                self._connected = False
                logger.error(f"[mux] Unexpected error: {e}")
                break

    async def _reattach_all(self):
        """Re-ATTACH all registered sessions after reconnect."""
        for sid, role in list(self._session_roles.items()):
            try:
                attach_frame = encode(Frame(
                    session=sid,
                    role=role,
                    type="attach",
                ))
                await self._ws.send_str(attach_frame)
                logger.info(f"[mux] Re-attached session '{sid}'")
            except Exception as e:
                logger.error(f"[mux] Failed to re-attach '{sid}': {e}")

    async def _flush_offline_buffers(self):
        """Flush buffered OUTPUT frames after reconnect (N2)."""
        for sid, buf in self._offline_buffers.items():
            if buf:
                try:
                    frame_str = encode(Frame(
                        session=sid,
                        role=self._session_roles.get(sid, "client"),
                        type=FrameType.OUTPUT.value,
                        data=bytes(buf),
                    ))
                    await self._ws.send_str(frame_str)
                    logger.info(f"[mux] Flushed {len(buf)} bytes offline buffer for '{sid}'")
                except Exception as e:
                    logger.error(f"[mux] Failed to flush buffer for '{sid}': {e}")
                buf.clear()

    # ---- Internal: send loop with priority (N5) ----

    async def _send_loop(self):
        """Drain priority queues and send frames. High priority first (N5)."""
        try:
            while self._connected and not self._stop_event.is_set():
                # Drain all high-priority first
                sent = False
                while not self._send_queue_high.empty():
                    frame_str = self._send_queue_high.get_nowait()
                    try:
                        await self._ws.send_str(frame_str)
                        self.metrics.bytes_sent += len(frame_str)
                    except Exception:
                        self._connected = False
                        return
                    sent = True

                # Then one low-priority if no high waiting
                if not sent and not self._send_queue_low.empty():
                    frame_str = self._send_queue_low.get_nowait()
                    try:
                        await self._ws.send_str(frame_str)
                        self.metrics.bytes_sent += len(frame_str)
                    except Exception:
                        self._connected = False
                        return

                # Wait briefly if nothing was sent
                if not sent and self._send_queue_low.empty():
                    try:
                        frame_str = await asyncio.wait_for(
                            self._send_queue_high.get(), timeout=0.05
                        )
                        try:
                            await self._ws.send_str(frame_str)
                            self.metrics.bytes_sent += len(frame_str)
                        except Exception:
                            self._connected = False
                            return
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            pass

    # ---- Internal: dispatch loop ----

    async def _dispatch_loop(self):
        """Read frames from WS and route to registered session queues."""
        try:
            async for msg in self._ws:
                if self._stop_event.is_set():
                    break
                if msg.type == WSMsgType.TEXT:
                    self.metrics.bytes_received += len(msg.data)
                    try:
                        frame = decode(msg.data)
                    except Exception as e:
                        logger.warning(f"[mux] Decode error: {e}")
                        continue

                    # Handle PONG for RTT measurement (N9) and dead detection (N3)
                    if frame.type == FrameType.PONG.value:
                        if self._ping_sent_at > 0:
                            rtt = (time.time() - self._ping_sent_at) * 1000
                            self.metrics.record_rtt(rtt)
                            self._ping_sent_at = 0
                        self._pong_miss_count = 0
                        self.metrics.pong_miss_count = 0
                        continue

                    # Route to session queue
                    queue = self._sessions.get(frame.session)
                    if queue is not None:
                        await queue.put(frame)
                    else:
                        logger.debug(f"[mux] No handler for session '{frame.session}' type={frame.type}")

                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[mux] WS error: {self._ws.exception()}")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[mux] Dispatch error: {e}")
        finally:
            self._connected = False

    # ---- Internal: heartbeat (N3) ----

    async def _heartbeat(self, interval: int = 10):
        """Application-level heartbeat with dead connection detection (N3)."""
        try:
            while self._connected and not self._stop_event.is_set():
                await asyncio.sleep(interval)
                if not self._connected or self._stop_event.is_set():
                    break

                # Check for missed pongs (N3)
                if self._pong_miss_count >= 2:
                    logger.warning("[mux] 2 consecutive pongs missed, forcing reconnect")
                    self.metrics.pong_miss_count = self._pong_miss_count
                    self._connected = False
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    break

                # Send ping to first registered session (or empty session)
                first_session = next(iter(self._sessions), "")
                first_role = self._session_roles.get(first_session, "client")
                try:
                    self._ping_sent_at = time.time()
                    self._pong_miss_count += 1
                    ping_frame = encode(Frame(
                        session=first_session,
                        role=first_role,
                        type="ping",
                    ))
                    await self._ws.send_str(ping_frame)
                except Exception:
                    self._connected = False
                    break
        except asyncio.CancelledError:
            pass
