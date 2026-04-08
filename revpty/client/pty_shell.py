import asyncio
import errno
import os
import subprocess
import shlex
from abc import ABC, abstractmethod
from revpty.platform_utils import IS_WINDOWS, default_shell

if not IS_WINDOWS:
    import pty
    import termios
    import fcntl
    import struct
    from select import select


class PTYBackend(ABC):
    """Abstract base class for platform-specific PTY backends."""

    @abstractmethod
    def start(self):
        """Spawn the shell process with a pseudo-terminal."""

    @abstractmethod
    async def read(self, size: int = 1024, timeout: float | None = None) -> bytes | None:
        """
        Read up to `size` bytes from the PTY.
        Returns bytes if data available, None on timeout, b"" on EOF.
        """

    @abstractmethod
    def write(self, data: bytes):
        """Write bytes to the PTY (sends to shell stdin)."""

    @abstractmethod
    def resize(self, rows: int, cols: int):
        """Resize the terminal dimensions."""

    @abstractmethod
    def stop(self):
        """Terminate the shell process and clean up."""

    @abstractmethod
    def is_running(self) -> bool:
        """Return True if the shell process is alive."""

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class PTYUnix(PTYBackend):
    """
    Unix PTY backend using pty/termios/fcntl.

    Manages a shell process using subprocess.Popen with pseudo-terminal (PTY).
    """

    def __init__(self, shell=None):
        self.shell = shell or default_shell()
        self.master = None
        self.slave = None
        self.process = None

    def start(self):
        """Start the shell process with a pseudo-terminal."""
        # Create pseudo-terminal pair
        self.master, self.slave = pty.openpty()

        attrs = termios.tcgetattr(self.slave)
        attrs[3] |= termios.ECHO | termios.ICANON | termios.ISIG
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("PS1", "\\u@\\h:\\w\\$ ")

        if isinstance(self.shell, str):
            shell_cmd = shlex.split(self.shell)
        else:
            shell_cmd = list(self.shell)

        if not shell_cmd:
            shell_cmd = [default_shell()]

        shell_name = os.path.basename(shell_cmd[0])
        if shell_name in ("bash", "zsh", "sh"):
            shell_cmd.append("-i")

        def _preexec():
            os.setsid()
            try:
                fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        # Start shell with PTY slave as stdin/stdout/stderr
        self.process = subprocess.Popen(
            shell_cmd,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            close_fds=True,
            # Don't buffer output
            bufsize=0,
            env=env,
            preexec_fn=_preexec,
        )

        # Close slave fd in parent - child has its own copy
        os.close(self.slave)
        self.slave = None

    async def read(self, size: int = 1024, timeout: float | None = None):
        """Asynchronously read data from the PTY master."""
        if self.master is None:
            raise RuntimeError("PTY master is not initialized")
        loop = asyncio.get_running_loop()

        def _read_with_timeout():
            ready, _, _ = select([self.master], [], [], timeout)
            if not ready:
                return None
            return os.read(self.master, size)

        try:
            if timeout is None:
                return await loop.run_in_executor(None, os.read, self.master, size)
            return await loop.run_in_executor(None, _read_with_timeout)
        except OSError as e:
            if e.errno == errno.EIO:
                return b""
            raise

    def write(self, data: bytes):
        """Write data to the PTY master (sends to shell stdin)."""
        if self.master is None:
            raise RuntimeError("PTY master is not initialized")
        os.write(self.master, data)

    def resize(self, rows: int, cols: int):
        if self.master is None:
            raise RuntimeError("PTY master is not initialized")
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)

    def stop(self):
        """Stop the shell process and clean up resources."""
        # Close the PTY master first
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

        # Terminate the process
        if self.process is not None:
            try:
                self.process.terminate()
                # Wait up to 1 second for graceful termination
                self.process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                # Force kill if it doesn't terminate gracefully
                try:
                    self.process.kill()
                except OSError:
                    pass
            self.process = None

    def is_running(self):
        """Check if the shell process is still running."""
        return self.process is not None and self.process.poll() is None

    def fileno(self):
        """Get the file descriptor of the PTY master."""
        return self.master


class PTYWindows(PTYBackend):
    """
    Windows PTY backend using pywinpty (ConPTY).
    """

    def __init__(self, shell=None):
        self.shell = shell or default_shell()
        self._proc = None

    def start(self):
        """Spawn the shell process with Windows ConPTY."""
        from winpty import PtyProcess

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        self._proc = PtyProcess.spawn(
            self.shell,
            env=env,
            dimensions=(24, 80),
        )

    async def read(self, size: int = 1024, timeout: float | None = None):
        """Asynchronously read data from the ConPTY."""
        if self._proc is None:
            raise RuntimeError("PTY not initialized")
        loop = asyncio.get_running_loop()

        def _read():
            try:
                data = self._proc.read(size)
                if data is None:
                    return b""
                return data.encode("utf-8", errors="replace")
            except EOFError:
                return b""
            except OSError:
                return b""

        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    loop.run_in_executor(None, _read), timeout=timeout
                )
            return await loop.run_in_executor(None, _read)
        except asyncio.TimeoutError:
            return None

    def write(self, data: bytes):
        """Write data to the ConPTY."""
        if self._proc is None:
            raise RuntimeError("PTY not initialized")
        self._proc.write(data.decode("utf-8", errors="replace"))

    def resize(self, rows: int, cols: int):
        """Resize the ConPTY terminal dimensions."""
        if self._proc is None:
            raise RuntimeError("PTY not initialized")
        self._proc.setwinsize(rows, cols)

    def stop(self):
        """Terminate the shell process and clean up."""
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
            self._proc = None

    def is_running(self):
        """Check if the shell process is still alive."""
        return self._proc is not None and self._proc.isalive()


def get_pty_backend(shell=None):
    """Factory: returns PTYUnix or PTYWindows based on the current platform."""
    if IS_WINDOWS:
        return PTYWindows(shell=shell)
    return PTYUnix(shell=shell)


# Backward-compatible alias
PTYShell = get_pty_backend
