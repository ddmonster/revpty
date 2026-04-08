import asyncio
import logging
import signal
import time
import os
import json
from revpty.protocol.frame import Frame, FrameType
from revpty.protocol.codec import encode, decode
from revpty.platform_utils import IS_WINDOWS, default_shell
from .pty_shell import PTYShell
from .file_manager import FileManager
from .mux import ConnectionMux
from .tunnel_proxy import TunnelProxy

_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class ShellWorker:
    """PTY worker that uses a shared ConnectionMux for communication."""

    def __init__(self, mux: ConnectionMux, session: str, shell: str = None, pty_factory=PTYShell):
        self.mux = mux
        self.session = session
        self.shell = shell or default_shell()
        self.mux = mux
        self.session = session
        self.shell = shell
        self.pty_factory = pty_factory
        self.file_manager = FileManager()
        self.running = True
        self._stop_event = asyncio.Event()
        self._shell_lock = asyncio.Lock()
        self.shell_instance = None
        self._queue: asyncio.Queue = None

    async def _ensure_shell(self):
        if self._stop_event.is_set():
            return
        async with self._shell_lock:
            if self.shell_instance and self.shell_instance.is_running():
                return
            if self.shell_instance:
                try:
                    self.shell_instance.stop()
                except Exception:
                    pass
            self.shell_instance = self.pty_factory(self.shell)
            self.shell_instance.start()
            logger.info(f"[+] PTY shell started for session '{self.session}'")

    async def _ws_to_pty(self):
        """Read frames from mux queue and forward to PTY."""
        try:
            while self.running and not self._stop_event.is_set():
                try:
                    frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if frame.type == FrameType.INPUT.value:
                    await self._ensure_shell()
                    if self.shell_instance:
                        self.shell_instance.write(frame.data)
                elif frame.type == FrameType.RESIZE.value:
                    if frame.rows is not None and frame.cols is not None:
                        await self._ensure_shell()
                        if self.shell_instance:
                            self.shell_instance.resize(frame.rows, frame.cols)
                elif frame.type == FrameType.FILE.value:
                    if frame.data:
                        response_data = await asyncio.to_thread(self.file_manager.handle_message, frame.data)
                        frame_str = encode(Frame(
                            session=self.session,
                            role="client",
                            type=FrameType.FILE.value,
                            data=response_data
                        ))
                        await self.mux.send(frame_str, FrameType.FILE.value)
                elif frame.type == FrameType.CONTROL.value:
                    if frame.data:
                        try:
                            payload = json.loads(frame.data.decode("utf-8"))
                        except Exception:
                            continue
                        if payload.get("op") == "close_shell":
                            ack = encode(Frame(
                                session=self.session,
                                role="client",
                                type=FrameType.CONTROL.value,
                                data=json.dumps({
                                    "op": "close_shell_ack",
                                    "session": self.session,
                                }).encode("utf-8")
                            ))
                            await self.mux.send(ack, FrameType.CONTROL.value)
                            self._stop_event.set()
                            break
                elif frame.type == FrameType.PING.value:
                    pong = encode(Frame(
                        session=self.session,
                        role="client",
                        type="pong"
                    ))
                    await self.mux.send(pong, FrameType.PONG.value)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Error in ShellWorker ws_to_pty: {e}")

    async def _pty_to_ws(self):
        """Read PTY output and send via mux."""
        try:
            while self.running and not self._stop_event.is_set():
                try:
                    await self._ensure_shell()
                    if not self.shell_instance:
                        await asyncio.sleep(0.2)
                        continue
                    data = await self.shell_instance.read(timeout=0.2)
                    if data is None:
                        continue
                    if not data:
                        await self._ensure_shell()
                        continue
                    frame_str = encode(Frame(
                        session=self.session,
                        role="client",
                        type="output",
                        data=data
                    ))
                    await self.mux.send(frame_str, FrameType.OUTPUT.value)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Error in ShellWorker pty_to_ws: {e}")

    async def run(self):
        """Start the worker: register with mux, run PTY I/O loops."""
        self._queue = self.mux.register(self.session)
        await self._ensure_shell()

        ws_task = asyncio.create_task(self._ws_to_pty())
        pty_task = asyncio.create_task(self._pty_to_ws())

        try:
            done, pending = await asyncio.wait(
                [ws_task, pty_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        finally:
            self.running = False
            self.mux.unregister(self.session)
            if self.shell_instance:
                try:
                    self.shell_instance.stop()
                except Exception:
                    pass

    async def stop(self):
        self._stop_event.set()
        self.running = False
        if self.shell_instance:
            try:
                self.shell_instance.stop()
            except Exception:
                pass


class Agent:
    """Main agent that uses ConnectionMux for all WS communication."""

    def __init__(self, server, session, shell=None, proxy=None, secret=None,
                 cf_client_id=None, cf_client_secret=None, insecure=False, tunnels=None, pty_factory=PTYShell):
        self.server = server
        self.session = session
        self.shell = shell or default_shell()
        self.proxy = proxy
        self.secret = secret
        self.cf_client_id = cf_client_id
        self.cf_client_secret = cf_client_secret
        self.insecure = insecure
        self.tunnels = tunnels or []  # List of tunnel specs: "port" or "host:port"
        self.running = True
        self.pty_factory = pty_factory
        self.file_manager = FileManager()
        self.tunnel_proxy = TunnelProxy()
        self._stop_event = asyncio.Event()
        self._shell_lock = asyncio.Lock()
        self.shell_instance = None
        self.shell_workers: dict[str, ShellWorker] = {}
        self.shell_worker_tasks: dict[str, asyncio.Task] = {}
        self._registered_tunnels: dict[str, dict] = {}  # tunnel_id -> {local_host, local_port}

        # ConnectionMux handles all WS communication
        self.mux = ConnectionMux(server, proxy=proxy, secret=secret,
                                  cf_client_id=cf_client_id, cf_client_secret=cf_client_secret,
                                  insecure=insecure)
        self._queue: asyncio.Queue = None

    def _parse_tunnel_spec(self, spec: str) -> tuple[str, int]:
        """Parse tunnel spec like '8080' or '127.0.0.1:8080' into (host, port)."""
        if ':' in spec:
            host, port = spec.rsplit(':', 1)
            return host, int(port)
        return "127.0.0.1", int(spec)

    async def _register_tunnels(self):
        """Register all tunnels with the server."""
        for spec in self.tunnels:
            try:
                local_host, local_port = self._parse_tunnel_spec(spec)
                frame = encode(Frame(
                    session=self.session,
                    role="client",
                    type=FrameType.CONTROL.value,
                    data=json.dumps({
                        "op": "tunnel_register",
                        "local_host": local_host,
                        "local_port": local_port
                    }).encode()
                ))
                await self.mux.send(frame, FrameType.CONTROL.value)
                logger.info(f"[tunnel] Registering tunnel -> {local_host}:{local_port}")
            except Exception as e:
                logger.error(f"[tunnel] Failed to register tunnel {spec}: {e}")

    async def _tunnel_registration_loop(self):
        """Periodically check and register tunnels when connected."""
        registered = set()
        while not self._stop_event.is_set():
            if self.mux.connected:
                # Register tunnels that haven't been registered yet
                for spec in self.tunnels:
                    if spec not in registered:
                        try:
                            local_host, local_port = self._parse_tunnel_spec(spec)
                            frame = encode(Frame(
                                session=self.session,
                                role="client",
                                type=FrameType.CONTROL.value,
                                data=json.dumps({
                                    "op": "tunnel_register",
                                    "local_host": local_host,
                                    "local_port": local_port
                                }).encode()
                            ))
                            await self.mux.send(frame, FrameType.CONTROL.value)
                            logger.info(f"[tunnel] Registered -> {local_host}:{local_port}")
                            registered.add(spec)
                        except Exception as e:
                            logger.error(f"[tunnel] Failed to register {spec}: {e}")
            else:
                # Clear registered set on disconnect so we re-register on reconnect
                registered.clear()
            await asyncio.sleep(2)

    async def _ensure_shell(self):
        if self._stop_event.is_set():
            return
        async with self._shell_lock:
            if self.shell_instance and self.shell_instance.is_running():
                return
            if self.shell_instance:
                try:
                    self.shell_instance.stop()
                except Exception:
                    pass
            self.shell_instance = self.pty_factory(self.shell)
            self.shell_instance.start()
            logger.info("[+] PTY shell started")

    async def _handle_control(self, frame):
        if not frame.data:
            return
        try:
            payload = json.loads(frame.data.decode("utf-8"))
        except Exception:
            return
        op = payload.get("op")
        if op == "tunnel_register_ack":
            # Server acknowledged tunnel registration
            tunnel_id = payload.get("tunnel_id")
            local_port = payload.get("local_port")
            if payload.get("ok"):
                logger.info(f"[tunnel] Registered tunnel_id={tunnel_id} for port {local_port}")
                self._registered_tunnels[tunnel_id] = {
                    "local_host": "127.0.0.1",
                    "local_port": local_port
                }
            else:
                logger.error(f"[tunnel] Registration failed for port {local_port}")
            return
        if op == "tunnel_request":
            # Phase 7: forward tunnel request to local service
            asyncio.create_task(self._handle_tunnel_request(payload))
            return
        if op == "new_shell":
            new_session = payload.get("session")
            shell_cmd = payload.get("shell") or self.shell
            if not new_session:
                return
            if new_session in self.shell_workers:
                ack = encode(Frame(
                    session=self.session,
                    role="client",
                    type=FrameType.CONTROL.value,
                    data=json.dumps({
                        "op": "new_shell_ack",
                        "session": new_session,
                        "ok": False,
                        "error": "session exists",
                    }).encode("utf-8")
                ))
                await self.mux.send(ack, FrameType.CONTROL.value)
                return
            worker = ShellWorker(
                mux=self.mux,
                session=new_session,
                shell=shell_cmd,
                pty_factory=self.pty_factory,
            )
            self.shell_workers[new_session] = worker
            task = asyncio.create_task(worker.run())
            self.shell_worker_tasks[new_session] = task

            def _cleanup(_, sid=new_session):
                self.shell_workers.pop(sid, None)
                self.shell_worker_tasks.pop(sid, None)
            task.add_done_callback(_cleanup)

            ack = encode(Frame(
                session=self.session,
                role="client",
                type=FrameType.CONTROL.value,
                data=json.dumps({
                    "op": "new_shell_ack",
                    "session": new_session,
                    "ok": True,
                }).encode("utf-8")
            ))
            await self.mux.send(ack, FrameType.CONTROL.value)

    async def _handle_tunnel_request(self, payload: dict):
        """Forward a tunnel request to the local service and send response back."""
        try:
            response_data = await self.tunnel_proxy.handle_request(payload)
            frame_str = encode(Frame(
                session=self.session,
                role="client",
                type=FrameType.CONTROL.value,
                data=response_data,
            ))
            await self.mux.send(frame_str, FrameType.CONTROL.value)
        except Exception as e:
            logger.error(f"[x] Tunnel request error: {e}")

    async def _ws_to_pty(self):
        """Read frames from mux queue and forward to PTY."""
        try:
            while self.running and not self._stop_event.is_set():
                try:
                    frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                logger.debug(f"[<] Frame: {frame.type} from {frame.role}, data_len={len(frame.data) if frame.data else 0}")

                if frame.type == FrameType.INPUT.value:
                    await self._ensure_shell()
                    if self.shell_instance:
                        self.shell_instance.write(frame.data)
                elif frame.type == FrameType.RESIZE.value:
                    if frame.rows is not None and frame.cols is not None:
                        await self._ensure_shell()
                        if self.shell_instance:
                            self.shell_instance.resize(frame.rows, frame.cols)
                elif frame.type == FrameType.FILE.value:
                    if frame.data:
                        response_data = await asyncio.to_thread(self.file_manager.handle_message, frame.data)
                        frame_str = encode(Frame(
                            session=self.session,
                            role="client",
                            type=FrameType.FILE.value,
                            data=response_data
                        ))
                        await self.mux.send(frame_str, FrameType.FILE.value)
                elif frame.type == FrameType.CONTROL.value:
                    await self._handle_control(frame)
                elif frame.type == FrameType.PING.value:
                    logger.debug("[<] Ping received, sending pong")
                    pong = encode(Frame(
                        session=self.session,
                        role="client",
                        type="pong"
                    ))
                    await self.mux.send(pong, FrameType.PONG.value)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Error in ws_to_pty: {e}")

    async def _pty_to_ws(self):
        """Forward PTY output via mux."""
        try:
            while self.running and not self._stop_event.is_set():
                try:
                    await self._ensure_shell()
                    if not self.shell_instance:
                        await asyncio.sleep(0.2)
                        continue
                    data = await self.shell_instance.read(timeout=0.2)
                    if data is None:
                        continue
                    if not data:
                        await self._ensure_shell()
                        continue
                    frame_str = encode(Frame(
                        session=self.session,
                        role="client",
                        type="output",
                        data=data
                    ))
                    await self.mux.send(frame_str, FrameType.OUTPUT.value)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Error in pty_to_ws: {e}")

    async def run(self):
        """Main agent entry point"""
        logger.info("[*] revpty client starting")
        logger.info(f"[*] Session: {self.session}")
        logger.info(f"[*] Shell: {self.shell}")
        logger.info(f"[*] Server: {self.server}")

        if self.proxy:
            logger.info(f"[*] Proxy: {self.proxy}")

        await self._ensure_shell()

        # Setup signal handlers
        def signal_handler():
            logger.info("[*] Received shutdown signal")
            self.running = False
            self._stop_event.set()

        loop = asyncio.get_running_loop()
        if IS_WINDOWS:
            loop.add_signal_handler(signal.SIGINT, signal_handler)
        else:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, signal_handler)

        try:
            # Register main session with mux and start mux
            self._queue = self.mux.register(self.session)
            await self.mux.start()

            # Start tunnel registration task (waits for connection then registers)
            tunnel_task = asyncio.create_task(self._tunnel_registration_loop())

            # Start PTY I/O tasks
            ws_to_pty_task = asyncio.create_task(self._ws_to_pty())
            pty_to_ws_task = asyncio.create_task(self._pty_to_ws())
            stop_task = asyncio.create_task(self._stop_event.wait())

            done, pending = await asyncio.wait(
                [ws_to_pty_task, pty_to_ws_task, stop_task, tunnel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except asyncio.CancelledError:
            logger.info("[*] Agent cancelled")
        except Exception as e:
            logger.error(f"[x] Agent error: {e}")
        finally:
            self.running = False
            self._stop_event.set()
            # Stop all shell workers
            for worker in list(self.shell_workers.values()):
                await worker.stop()
            # Close tunnel proxy
            await self.tunnel_proxy.close()
            # Close mux
            await self.mux.close()
            logger.info("[*] revpty client stopped")
            logger.info(f"[*] mux metrics: rtt={self.mux.metrics.rtt_ms:.0f}ms, "
                        f"sent={self.mux.metrics.bytes_sent}, recv={self.mux.metrics.bytes_received}, "
                        f"reconnects={self.mux.metrics.reconnect_count}")
