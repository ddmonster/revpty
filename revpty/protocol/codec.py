import json
import base64
import time
from .frame import Frame, PROTOCOL_VERSION


class ProtocolError(Exception):
    """Protocol validation error"""
    pass


def encode(frame: Frame) -> str:
    """Encode frame to JSON string"""
    # Add timestamp if not present
    if frame.ts is None:
        frame.ts = time.time()
    
    obj = {
        "v": frame.v,
        "session": frame.session,
        "role": frame.role,
        "type": frame.type,
        "ts": frame.ts,
    }
    
    # Add optional fields
    if frame.data is not None:
        obj["data"] = base64.b64encode(frame.data).decode()
    
    if frame.rows is not None:
        obj["rows"] = frame.rows
    
    if frame.cols is not None:
        obj["cols"] = frame.cols
    
    return json.dumps(obj)


def decode(raw: str) -> Frame:
    """Decode JSON string to frame with validation
    
    Raises:
        ProtocolError: If frame is invalid
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Invalid JSON: {e}")
    
    # Check required fields
    required_fields = ["v", "session", "role", "type"]
    missing = [f for f in required_fields if f not in obj]
    if missing:
        raise ProtocolError(f"Missing required fields: {missing}")
    
    # Decode data if present
    if "data" in obj:
        try:
            obj["data"] = base64.b64decode(obj["data"])
        except Exception as e:
            raise ProtocolError(f"Invalid base64 data: {e}")
    
    # Create frame
    frame = Frame(
        v=obj["v"],
        session=obj["session"],
        role=obj["role"],
        type=obj["type"],
        data=obj.get("data"),
        rows=obj.get("rows"),
        cols=obj.get("cols"),
        ts=obj.get("ts")
    )
    
    # Validate frame
    is_valid, error_msg = frame.validate()
    if not is_valid:
        raise ProtocolError(f"Frame validation failed: {error_msg}")
    
    return frame
