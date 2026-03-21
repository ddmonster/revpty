"""Output ring buffer for server-side session caching"""


class OutputRingBuffer:
    """Fixed-capacity buffer that drops oldest data when full.

    Used to cache recent terminal output so that newly-attached
    browsers/viewers can see session history immediately.
    """

    def __init__(self, capacity: int = 131072):
        self._buf = bytearray()
        self.capacity = capacity

    def append(self, data: bytes):
        """Append data, truncating the front if over capacity."""
        self._buf.extend(data)
        overflow = len(self._buf) - self.capacity
        if overflow > 0:
            del self._buf[:overflow]

    def get_all(self) -> bytes:
        """Return all buffered content."""
        return bytes(self._buf)

    def clear(self):
        """Clear the buffer."""
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)

    def __bool__(self) -> bool:
        return len(self._buf) > 0
