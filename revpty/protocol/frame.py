from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum


class FrameType(Enum):
    """Protocol frame types with validation rules"""
    ATTACH = "attach"       # Bind to session
    DETACH = "detach"       # Unbind from session
    INPUT = "input"         # stdin (requires data)
    OUTPUT = "output"       # stdout (requires data)
    RESIZE = "resize"       # TTY resize (requires rows, cols)
    PING = "ping"           # Heartbeat
    PONG = "pong"           # Heartbeat response
    STATUS = "status"       # Session/peer status message
    FILE = "file"           # File operation (list/read/write)
    CONTROL = "control"     # Session control operations
    
    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls._value2member_map_


class Role(Enum):
    """Connection roles"""
    SERVER = "server"       # System messages
    CLIENT = "client"       # PTY owner
    BROWSER = "browser"     # Terminal user
    VIEWER = "viewer"       # Read-only observer
    AGENT = "agent"         # AI/automation (future)


PROTOCOL_VERSION = 1


@dataclass
class Frame:
    """Protocol frame with version and validation"""
    v: int = PROTOCOL_VERSION
    session: str = ""
    role: str = ""
    type: str = ""
    data: Optional[bytes] = None
    rows: Optional[int] = None
    cols: Optional[int] = None
    ts: Optional[float] = None  # Timestamp for debugging/auditing
    
    def validate(self) -> tuple[bool, Optional[str]]:
        """Validate frame fields and constraints
        
        Returns:
            (is_valid, error_message)
        """
        # Check protocol version
        if self.v != PROTOCOL_VERSION:
            return False, f"Unsupported protocol version: {self.v} (expected {PROTOCOL_VERSION})"
        
        # Check frame type
        if not FrameType.is_valid(self.type):
            return False, f"Invalid frame type: {self.type}"
        
        # Check role
        if self.role not in [r.value for r in Role]:
            return False, f"Invalid role: {self.role}"
        
        # Check field constraints based on type
        frame_type = FrameType(self.type)
        
        if frame_type == FrameType.INPUT:
            if not self.data:
                return False, "INPUT frame requires data field"
            if self.rows is not None or self.cols is not None:
                return False, "INPUT frame should not have rows/cols"
        
        elif frame_type == FrameType.OUTPUT:
            if not self.data:
                return False, "OUTPUT frame requires data field"
            if self.rows is not None or self.cols is not None:
                return False, "OUTPUT frame should not have rows/cols"
        
        elif frame_type == FrameType.RESIZE:
            if self.rows is None or self.cols is None:
                return False, "RESIZE frame requires both rows and cols"
            if self.data is not None:
                return False, "RESIZE frame should not have data"
        
        elif frame_type in (FrameType.ATTACH, FrameType.DETACH, 
                           FrameType.PING, FrameType.PONG):
            if self.data is not None:
                return False, f"{self.type.upper()} frame should not have data"
            if self.rows is not None or self.cols is not None:
                return False, f"{self.type.upper()} frame should not have rows/cols"
        
        elif frame_type == FrameType.STATUS:
            if self.rows is not None or self.cols is not None:
                return False, "STATUS frame should not have rows/cols"
        
        elif frame_type == FrameType.FILE:
            if not self.data:
                return False, "FILE frame requires data field"
        
        elif frame_type == FrameType.CONTROL:
            if not self.data:
                return False, "CONTROL frame requires data field"
        
        return True, None
