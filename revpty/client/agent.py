import asyncio
import logging
import signal
import random
import time
import os
import json
import aiohttp
from aiohttp import ClientSession, WSMsgType
from revpty.protocol.frame import Frame, FrameType
from revpty.protocol.codec import encode, decode
from .pty_shell import PTYShell
from .file_manager import FileManager

_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class ShellWorker:
    def __init__(self, server, session, shell="/bin/bash", proxy=None, secret=None, pty_factory=PTYShell):
        self.server = server
        self.session = session
        self.shell = shell
        self.proxy = proxy
        self.secret = secret
        self.pty_factory = pty_factory
        self.file_manager = FileManager()
        self.running = True
        self.connected = False
        self._stop_event = asyncio.Event()
        self._connect_task = None
        self._shell_lock = asyncio.Lock()
        self.shell_instance = None

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

    async def _heartbeat(self, ws, interval=30):
        try:
            while self.connected and not self._stop_event.is_set():
                await asyncio.sleep(interval)
                if self.connected and not self._stop_event.is_set():
                    await ws.send_str(encode(Frame(
                        session=self.session,
                        role="client",
                        type="ping"
                    )))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Heartbeat error: {e}")
            self.connected = False

    async def _ws_to_pty(self, ws):
        try:
            async for msg in ws:
                if not self.connected or self._stop_event.is_set():
                    break
                if msg.type == WSMsgType.TEXT:
                    frame = decode(msg.data)
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
                            await ws.send_str(encode(Frame(
                                session=self.session,
                                role="client",
                                type=FrameType.FILE.value,
                                data=response_data
                            )))
                    elif frame.type == FrameType.CONTROL.value:
                        if frame.data:
                            try:
                                payload = json.loads(frame.data.decode("utf-8"))
                            except Exception:
                                continue
                            if payload.get("op") == "close_shell":
                                await ws.send_str(encode(Frame(
                                    session=self.session,
                                    role="client",
                                    type=FrameType.CONTROL.value,
                                    data=json.dumps({
                                        "op": "close_shell_ack",
                                        "session": self.session,
                                    }).encode("utf-8")
                                )))
                                self._stop_event.set()
                                break
                    elif frame.type == FrameType.PING.value:
                        await ws.send_str(encode(Frame(
                            session=self.session,
                            role="client",
                            type="pong"
                        )))
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[x] WebSocket error: {ws.exception()}")
                    break
                elif msg.type == WSMsgType.CLOSED:
                    break
        except Exception as e:
            logger.error(f"[x] Error in ws_to_pty: {e}")
        finally:
            self.connected = False

    async def _pty_to_ws(self, ws):
        try:
            while self.connected and not self._stop_event.is_set():
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
                    await ws.send_str(encode(Frame(
                        session=self.session,
                        role="client",
                        type="output",
                        data=data
                    )))
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            logger.error(f"[x] Error in pty_to_ws: {e}")
        finally:
            self.connected = False

    async def _connect_with_retry(self):
        retry_count = 0
        retry_delay = 1
        while self.running and not self._stop_event.is_set():
            try:
                proxy_info = f" via {self.proxy}" if self.proxy else ""
                headers = {"X-Revpty-Secret": self.secret} if self.secret else None
                timeout = aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                    sock_connect=10,
                    sock_read=30
                )
                async with ClientSession(timeout=timeout) as http_session:
                    logger.info(f"[*] Connecting to {self.server}{proxy_info}... (attempt #{retry_count + 1})")
                    async with http_session.ws_connect(
                        self.server,
                        proxy=self.proxy,
                        headers=headers,
                        heartbeat=30,
                        compress=False
                    ) as ws:
                        retry_count = 0
                        retry_delay = 1
                        self.connected = True
                        if self._stop_event.is_set():
                            await ws.close()
                            break
                        await ws.send_str(encode(Frame(
                            session=self.session,
                            role="client",
                            type="attach"
                        )))
                        logger.info(f"[+] Connected to session '{self.session}'")
                        heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                        ws_to_pty_task = asyncio.create_task(self._ws_to_pty(ws))
                        pty_to_ws_task = asyncio.create_task(self._pty_to_ws(ws))
                        done, pending = await asyncio.wait(
                            [heartbeat_task, ws_to_pty_task, pty_to_ws_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        self.connected = False
                        if self._stop_event.is_set():
                            try:
                                await ws.close()
                            except Exception:
                                pass
                            break
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.connected = False
                retry_count += 1
                logger.warning(f"[!] Connection failed: {type(e).__name__}: {e}")
                logger.info(f"[*] Reconnecting in {retry_delay}s... (attempt #{retry_count})")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                retry_delay = min(retry_delay * 2, 30)
                retry_delay = retry_delay + random.uniform(0, 0.5)
            except Exception as e:
                self.connected = False
                logger.error(f"[x] Unexpected error: {e}")
                break

    async def run(self):
        self._connect_task = asyncio.create_task(self._connect_with_retry())
        await self._stop_event.wait()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        self.running = False
        if self.shell_instance:
            try:
                self.shell_instance.stop()
            except Exception:
                pass

    async def stop(self):
        self._stop_event.set()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        if self.shell_instance:
            try:
                self.shell_instance.stop()
            except Exception:
                pass


class Agent:
    def __init__(self, server, session, shell="/bin/bash", proxy=None, secret=None, pty_factory=PTYShell):
        self.server = server
        self.session = session
        self.shell = shell
        self.proxy = proxy
        self.secret = secret
        self.running = True
        self.connected = False
        self.pty_factory = pty_factory
        self.metrics = {
            "connect_attempts": 0,
            "connect_successes": 0,
            "connect_failures": 0,
            "last_connect_ms": None,
        }
        self.file_manager = FileManager()
        self._stop_event = asyncio.Event()
        self._connect_task = None
        self._shell_lock = asyncio.Lock()
        self.shell_instance = None
        self.shell_workers = {}
        self.shell_worker_tasks = {}

    async def _heartbeat(self, ws, interval=30):
        """Send periodic heartbeat to keep connection alive"""
        try:
            while self.connected and not self._stop_event.is_set():
                await asyncio.sleep(interval)
                if self.connected and not self._stop_event.is_set():
                    await ws.send_str(encode(Frame(
                        session=self.session,
                        role="client",
                        type="ping"
                    )))
                    logger.debug("[>] Heartbeat sent")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[x] Heartbeat error: {e}")
            self.connected = False

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
            logger.info(f"[+] PTY shell started")

    async def _handle_control(self, ws, frame):
        if not frame.data:
            return
        try:
            payload = json.loads(frame.data.decode("utf-8"))
        except Exception:
            return
        op = payload.get("op")
        if op == "new_shell":
            new_session = payload.get("session")
            shell_cmd = payload.get("shell") or self.shell
            if not new_session:
                return
            if new_session in self.shell_workers:
                await ws.send_str(encode(Frame(
                    session=self.session,
                    role="client",
                    type=FrameType.CONTROL.value,
                    data=json.dumps({
                        "op": "new_shell_ack",
                        "session": new_session,
                        "ok": False,
                        "error": "session exists",
                    }).encode("utf-8")
                )))
                return
            worker = ShellWorker(
                server=self.server,
                session=new_session,
                shell=shell_cmd,
                proxy=self.proxy,
                secret=self.secret,
                pty_factory=self.pty_factory,
            )
            self.shell_workers[new_session] = worker
            task = asyncio.create_task(worker.run())
            self.shell_worker_tasks[new_session] = task
            def _cleanup(_):
                self.shell_workers.pop(new_session, None)
                self.shell_worker_tasks.pop(new_session, None)
            task.add_done_callback(_cleanup)
            await ws.send_str(encode(Frame(
                session=self.session,
                role="client",
                type=FrameType.CONTROL.value,
                data=json.dumps({
                    "op": "new_shell_ack",
                    "session": new_session,
                    "ok": True,
                }).encode("utf-8")
            )))

    async def _ws_to_pty(self, ws):
        """Forward WebSocket messages to PTY"""
        try:
            async for msg in ws:
                if not self.connected or self._stop_event.is_set():
                    break
                    
                if msg.type == WSMsgType.TEXT:
                    frame = decode(msg.data)
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
                        # Handle file operation
                        if frame.data:
                            response_data = await asyncio.to_thread(self.file_manager.handle_message, frame.data)
                            await ws.send_str(encode(Frame(
                                session=self.session,
                                role="client",
                                type=FrameType.FILE.value,
                                data=response_data
                            )))
                    elif frame.type == FrameType.CONTROL.value:
                        await self._handle_control(ws, frame)
                    elif frame.type == FrameType.PING.value:
                        logger.debug("[<] Ping received, sending pong")
                        await ws.send_str(encode(Frame(
                            session=self.session,
                            role="client",
                            type="pong"
                        )))
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[x] WebSocket error: {ws.exception()}")
                    break
                elif msg.type == WSMsgType.CLOSED:
                    logger.warning("[!] Connection closed by server")
                    break
        except Exception as e:
            logger.error(f"[x] Error in ws_to_pty: {e}")
        finally:
            self.connected = False

    async def _pty_to_ws(self, ws):
        """Forward PTY output to WebSocket"""
        try:
            while self.connected and not self._stop_event.is_set():
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
                    await ws.send_str(encode(Frame(
                        session=self.session,
                        role="client",
                        type="output",
                        data=data
                    )))
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            logger.error(f"[x] Error in pty_to_ws: {e}")
        finally:
            self.connected = False

    async def _connect_with_retry(self):
        """Connect to server with infinite retry"""
        retry_count = 0
        retry_delay = 1
        
        while self.running and not self._stop_event.is_set():
            try:
                proxy_info = f" via {self.proxy}" if self.proxy else ""
                self.metrics["connect_attempts"] += 1
                connect_start = time.perf_counter()
                
                # Create session and headers once outside loop if possible, but we retry connection...
                # Actually, headers are static so we can define them before.
                headers = {"X-Revpty-Secret": self.secret} if self.secret else None
                
                timeout = aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                    sock_connect=10,
                    sock_read=30
                )
                
                async with ClientSession(timeout=timeout) as http_session:
                    logger.info(f"[*] Connecting to {self.server}{proxy_info}... (attempt #{retry_count + 1})")
                    async with http_session.ws_connect(
                        self.server, 
                        proxy=self.proxy,
                        headers=headers,
                        heartbeat=30,
                        compress=False
                    ) as ws:
                        self.metrics["last_connect_ms"] = round((time.perf_counter() - connect_start) * 1000, 2)
                        retry_count = 0
                        retry_delay = 1
                        self.connected = True
                        self.metrics["connect_successes"] += 1
                        if self._stop_event.is_set():
                            await ws.close()
                            break
                        
                        # Send attach frame
                        await ws.send_str(encode(Frame(
                            session=self.session,
                            role="client",
                            type="attach"
                        )))
                        logger.info(f"[+] Connected to session '{self.session}'")
                        
                        # Start tasks
                        heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                        ws_to_pty_task = asyncio.create_task(self._ws_to_pty(ws))
                        pty_to_ws_task = asyncio.create_task(self._pty_to_ws(ws))
                        
                        # Wait for any task to finish
                        done, pending = await asyncio.wait(
                            [heartbeat_task, ws_to_pty_task, pty_to_ws_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        
                        # Cancel remaining tasks
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        
                        self.connected = False
                        if self._stop_event.is_set():
                            try:
                                await ws.close()
                            except Exception:
                                pass
                            break
                        
            except asyncio.CancelledError:
                logger.info("[*] Connection cancelled")
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.connected = False
                retry_count += 1
                self.metrics["connect_failures"] += 1
                logger.warning(f"[!] Connection failed: {type(e).__name__}: {e}")
                logger.info(f"[*] Reconnecting in {retry_delay}s... (attempt #{retry_count})")
                
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                
                # Exponential backoff with jitter
                retry_delay = min(retry_delay * 2, 30)
                retry_delay = retry_delay + random.uniform(0, 0.5)
            except Exception as e:
                self.connected = False
                logger.error(f"[x] Unexpected error: {e}")
                break

    async def run(self):
        """Main agent entry point"""
        logger.info(f"[*] revpty client starting")
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
            self.connected = False
            self._stop_event.set()
            if self._connect_task:
                self._connect_task.cancel()
        
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, signal_handler)
        
        try:
            self._connect_task = asyncio.create_task(self._connect_with_retry())
            stop_task = asyncio.create_task(self._stop_event.wait())
            done, pending = await asyncio.wait(
                [self._connect_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                if self._connect_task and not self._connect_task.done():
                    self._connect_task.cancel()
                    try:
                        await self._connect_task
                    except asyncio.CancelledError:
                        pass
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
            self.connected = False
            self._stop_event.set()
            for worker in list(self.shell_workers.values()):
                await worker.stop()
            logger.info("[*] revpty client stopped")
            logger.info(f"[*] client metrics: {self.metrics}")
