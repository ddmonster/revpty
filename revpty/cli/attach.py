import asyncio
import logging
import aiohttp
import os
from aiohttp import WSMsgType
import sys
import tty
import termios
import os
import random
import time
import json
import uuid
from enum import Enum
import signal
from select import select

from revpty.protocol.frame import Frame, FrameType
from revpty.protocol.codec import encode, decode


_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class AttachState(Enum):
    INIT = "init"
    ATTACH_SENT = "attach_sent"
    ACTIVE = "active"
    DETACHED = "detached"
    CLOSED = "closed"


class InteractiveTerminal:
    def __init__(self, ws, session: str, attach_id: str):
        self.ws = ws
        self.session = session
        self.attach_id = attach_id
        self._old_tty = None
        self._running = True
        self._detached_by_user = False
        self._status_event = asyncio.Event()
        self._last_output = None
        self.state = AttachState.INIT
        self._sigwinch_enabled = False

    # ---------- TTY handling ----------

    def setup_terminal(self):
        if not sys.stdin.isatty():
            return
        self._old_tty = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())

    def restore_terminal(self):
        if self._old_tty:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_tty)
            print()

    # ---------- Protocol helpers ----------

    async def send_frame(self, frame: Frame):
        await self.ws.send_str(encode(frame))

    # ---------- Main loop ----------

    async def run(self):
        self.setup_terminal()
        loop = asyncio.get_running_loop()

        logger.info(f"[+] Attached to session '{self.session}'")
        logger.info("[*] Press Ctrl+] to detach")

        try:
            ws_task = asyncio.create_task(self._read_from_ws())
            await self.send_frame(
                Frame(
                    session=self.session,
                    role="browser",
                    type="attach",
                )
            )
            self.state = AttachState.ATTACH_SENT
            await self._send_resize()
            if sys.stdin.isatty():
                loop.add_signal_handler(signal.SIGWINCH, lambda: asyncio.create_task(self._send_resize()))
                self._sigwinch_enabled = True

            try:
                await asyncio.wait_for(self._status_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[!] Attach status timeout")
            self.state = AttachState.ACTIVE
            if self._last_output is None:
                await self.send_frame(Frame(
                    session=self.session,
                    role="browser",
                    type=FrameType.INPUT.value,
                    data=b"\n",
                ))

            stdin_task = asyncio.create_task(self._read_from_stdin())
            heartbeat_task = asyncio.create_task(self._heartbeat())

            done, pending = await asyncio.wait(
                {ws_task, stdin_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

        except KeyboardInterrupt:
            logger.info("\n[*] Interrupted by user")

        except Exception as e:
            logger.error(f"[x] Terminal error: {e}")

        finally:
            try:
                await self.send_frame(
                    Frame(
                        session=self.session,
                        role="browser",
                        type="detach",
                    )
                )
            except Exception:
                pass
            self.state = AttachState.DETACHED
            self.restore_terminal()
            logger.info(f"[-] Detached from session '{self.session}'")
            if self._sigwinch_enabled:
                loop.remove_signal_handler(signal.SIGWINCH)
        
        self.state = AttachState.CLOSED
        return "user_detach" if self._detached_by_user else "ws_closed"

    # ---------- WS → stdout ----------

    async def _read_from_ws(self):
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    frame = decode(msg.data)

                    if frame.type == FrameType.OUTPUT.value:
                        sys.stdout.buffer.write(frame.data)
                        sys.stdout.buffer.flush()
                        self._last_output = time.time()
                    elif frame.type == FrameType.PING.value:
                        await self.send_frame(Frame(
                            session=self.session,
                            role="browser",
                            type=FrameType.PONG.value,
                        ))
                    elif frame.type == FrameType.STATUS.value:
                        self._status_event.set()
                        try:
                            payload = json.loads(frame.data.decode("utf-8")) if frame.data else {}
                            logger.info(f"[*] Attach status: {payload}")
                        except Exception:
                            logger.info("[*] Attach status received")

                elif msg.type == WSMsgType.CLOSED:
                    logger.warning("\r[!] Connection closed by server")
                    break

                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"\r[x] WebSocket error: {self.ws.exception()}")
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"\r[x] WS read error: {e}")

    # ---------- stdin → WS ----------

    async def _read_from_stdin(self):
        fd = sys.stdin.fileno()
        loop = asyncio.get_running_loop()

        try:
            while True:
                # wait for stdin readable
                await loop.run_in_executor(None, select, [fd], [], [])

                data = os.read(fd, 1024)
                if not data:
                    break

                # Ctrl+] detach
                if b"\x1d" in data:
                    logger.info("\n[*] Detaching from session...")
                    self._detached_by_user = True
                    break

                await self.send_frame(
                    Frame(
                        session=self.session,
                        role="browser",
                        type=FrameType.INPUT.value,
                        data=data,
                    )
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"\r[x] stdin read error: {e}")

    async def _send_resize(self):
        if not sys.stdin.isatty():
            return
        size = os.get_terminal_size(sys.stdin.fileno())
        await self.send_frame(Frame(
            session=self.session,
            role="browser",
            type=FrameType.RESIZE.value,
            rows=size.lines,
            cols=size.columns,
        ))

    async def _heartbeat(self, interval: int = 20):
        try:
            while True:
                await asyncio.sleep(interval)
                await self.send_frame(Frame(
                    session=self.session,
                    role="browser",
                    type=FrameType.PING.value,
                ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"\r[x] heartbeat error: {e}")


# ---------- Public API ----------

async def attach(server: str, session: str, proxy: str | None = None, secret: str | None = None):
    proxy_info = f" via {proxy}" if proxy else ""
    logger.info(f"[*] Connecting to {server}{proxy_info}")
    attach_id = uuid.uuid4().hex[:10]
    attempt = 0
    retry_delay = 1
    metrics = {
        "attach_id": attach_id,
        "attempts": 0,
        "failures": 0,
        "start_ms": round(time.time() * 1000),
    }

    while True:
        attempt += 1
        metrics["attempts"] = attempt
        try:
            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=10,
                sock_connect=10,
                sock_read=30
            )
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                headers = None
                if secret:
                    headers = {"X-Revpty-Secret": secret}
                async with http_session.ws_connect(server, proxy=proxy, headers=headers, heartbeat=30) as ws:
                    term = InteractiveTerminal(ws, session, attach_id)
                    result = await term.run()
                    if result == "user_detach":
                        break
        except aiohttp.ClientError as e:
            metrics["failures"] += 1
            logger.error(f"[x] Connection failed: {e}")
        except Exception as e:
            metrics["failures"] += 1
            logger.error(f"[x] Unexpected error: {e}")
        logger.info(f"[*] Reconnecting in {retry_delay}s...")
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30) + random.uniform(0, 0.5)

    logger.info("[*] Attach session ended")
    logger.info(f"[*] attach metrics: {metrics}")
