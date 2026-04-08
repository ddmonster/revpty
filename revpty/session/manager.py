"""
Session Management - Phase 1

Core principle: Session lifecycle > WebSocket lifecycle
"""
import asyncio
import logging
import time
from enum import Enum
from typing import Set, Optional
from dataclasses import dataclass
from .buffer import OutputRingBuffer
from ..platform_utils import default_shell

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Session lifecycle states"""
    INIT = "init"           # Initial state, no PTY yet
    RUNNING = "running"     # PTY running, accepting connections
    DEAD = "dead"           # PTY terminated, session cleanup


@dataclass
class SessionConfig:
    """Session configuration"""
    shell: str = default_shell()
    idle_timeout: int = 3600  # seconds before auto-cleanup
    enable_log: bool = True
    output_cache_size: int = 131072  # 128KB output cache for browser replay


class Session:
    """
    Shell session with independent lifecycle
    
    Core principles:
    - Session owns the PTY, not WebSocket
    - Multiple clients/browsers can attach/detach
    - Session persists across disconnections
    """
    
    def __init__(self, session_id: str, config: SessionConfig):
        self.id = session_id
        self.config = config
        self.state = SessionState.INIT
        
        # PTY (will be created when client attaches)
        self.pty = None
        
        # Connections
        self.clients: Set = set()
        self.browsers: Set = set()
        self.viewers: Set = set()
        
        # Output cache for browser replay
        self.output_buffer = OutputRingBuffer(config.output_cache_size)
        
        # Metadata
        self.created_at = time.time()
        self.last_active = time.time()
        self.attach_count = 0
        
        logger.info(f"[*] Session '{session_id}' created")
    
    async def start_pty(self, pty_factory):
        """Start PTY when client attaches"""
        if self.pty is not None:
            logger.warning(f"[!] Session '{self.id}' already has PTY")
            return
        
        self.pty = pty_factory(self.config.shell)
        self.pty.start()
        self.state = SessionState.RUNNING
        self.last_active = time.time()
        
        logger.info(f"[+] PTY started for session '{self.id}'")
    
    def attach(self, role: str, ws):
        """Attach a WebSocket to this session"""
        self.last_active = time.time()
        self.attach_count += 1
        
        # N8: Clean stale (closed) WebSockets before adding new one
        self.clients = {c for c in self.clients if not c.closed}
        self.browsers = {b for b in self.browsers if not b.closed}
        self.viewers = {v for v in self.viewers if not v.closed}
        
        if role == "client":
            self.clients.add(ws)
            logger.info(f"[+] CLIENT attached to session '{self.id}' (total: {len(self.clients)})")
        elif role == "browser":
            self.browsers.add(ws)
            logger.info(f"[+] BROWSER attached to session '{self.id}' (total: {len(self.browsers)})")
        elif role == "viewer":
            self.viewers.add(ws)
            logger.info(f"[+] VIEWER attached to session '{self.id}' (total: {len(self.viewers)})")
        
        self._log_status()
    
    def detach(self, role: str, ws):
        """Detach a WebSocket from this session"""
        if role == "client" and ws in self.clients:
            self.clients.remove(ws)
            logger.info(f"[-] CLIENT detached from session '{self.id}' (remaining: {len(self.clients)})")
        elif role == "browser" and ws in self.browsers:
            self.browsers.remove(ws)
            logger.info(f"[-] BROWSER detached from session '{self.id}' (remaining: {len(self.browsers)})")
        elif role == "viewer" and ws in self.viewers:
            self.viewers.remove(ws)
            logger.info(f"[-] VIEWER detached from session '{self.id}' (remaining: {len(self.viewers)})")
        
        self.last_active = time.time()
        self._log_status()
    
    def get_peer(self, role: str):
        """Get peer WebSocket(s) for a role"""
        if role == "client":
            # Clients talk to browsers AND viewers (output)
            return self.browsers | self.viewers
        elif role == "browser":
            # Browsers talk to clients
            return self.clients
        elif role == "viewer":
            # Viewers talk to clients (for status/files if allowed, but read-only enforced elsewhere)
            return self.clients
        return set()
    
    def is_empty(self) -> bool:
        """Check if session has no active connections"""
        return len(self.clients) == 0 and len(self.browsers) == 0 and len(self.viewers) == 0
    
    def is_idle(self) -> bool:
        """Check if session has exceeded idle timeout"""
        idle_time = time.time() - self.last_active
        return idle_time > self.config.idle_timeout
    
    async def close(self):
        """Close PTY and mark session as dead"""
        if self.state == SessionState.DEAD:
            return
        
        self.state = SessionState.DEAD
        
        # Close PTY
        if self.pty:
            try:
                if hasattr(self.pty, "stop"):
                    self.pty.stop()
            except Exception as e:
                logger.error(f"[x] Error closing PTY: {e}")
            self.pty = None
        
        # Clear output cache
        self.output_buffer.clear()
        
        logger.info(f"[x] Session '{self.id}' closed (lifespan: {int(time.time() - self.created_at)}s)")
    
    def _log_status(self):
        """Log current session status"""
        if self.state == SessionState.RUNNING:
            status = f"clients={len(self.clients)}, browsers={len(self.browsers)}, viewers={len(self.viewers)}, pty=RUNNING"
        else:
            status = f"clients={len(self.clients)}, browsers={len(self.browsers)}, viewers={len(self.viewers)}, pty=STOPPED"
        logger.debug(f"[*] Session '{self.id}': {status}")


class SessionManager:
    """
    Manages multiple sessions with lifecycle
    
    Core responsibilities:
    - Create/get sessions
    - Attach/detach connections
    - Cleanup idle/dead sessions
    """
    
    def __init__(self, pty_factory, config: SessionConfig = None):
        self.pty_factory = pty_factory
        self.config = config or SessionConfig()
        self.sessions: dict[str, Session] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def get_or_create(self, session_id: str) -> Session:
        """Get existing session or create new one"""
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id, self.config)
        return self.sessions[session_id]
    
    def get(self, session_id: str) -> Optional[Session]:
        """Get session if exists"""
        return self.sessions.get(session_id)
    
    async def attach(self, session_id: str, role: str, ws) -> Session:
        """Attach WebSocket to session, creating if needed"""
        session = self.get_or_create(session_id)
        
        # Start PTY if client attaches and PTY not running
        if role == "client" and session.state == SessionState.INIT:
            await session.start_pty(self.pty_factory)
        
        session.attach(role, ws)
        return session
    
    def detach(self, session_id: str, role: str, ws):
        """Detach WebSocket from session"""
        session = self.get(session_id)
        if session:
            session.detach(role, ws)
    
    async def cleanup_idle(self):
        """Close and remove idle sessions"""
        to_remove = []
        for sid, session in self.sessions.items():
            if session.is_empty() and session.is_idle():
                to_remove.append(sid)
        
        for sid in to_remove:
            await self.close_session(sid)
    
    async def close_session(self, session_id: str):
        """Close and remove a session"""
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()
    
    async def start_cleanup_task(self, interval: int = 60):
        """Start periodic cleanup task"""
        async def cleanup_loop():
            while True:
                await asyncio.sleep(interval)
                await self.cleanup_idle()
        
        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info("[+] Session cleanup task started")
    
    async def shutdown(self):
        """Close all sessions"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        for sid in list(self.sessions.keys()):
            await self.close_session(sid)
        
        logger.info("[*] SessionManager shut down")
