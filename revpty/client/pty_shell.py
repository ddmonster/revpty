import asyncio
import errno
import os
import pty
import subprocess
import shlex
from select import select
import termios
import fcntl
import struct


class PTYShell:
    """
    Manages a shell process using subprocess.Popen with pseudo-terminal (PTY).

    This approach provides better process management and control compared to pty.fork().
    """

    def __init__(self, shell="/bin/bash"):
        """
        Initialize the PTYShell.

        Args:
            shell: Path to the shell executable (default: /bin/bash)
        """
        self.shell = shell
        self.master = None
        self.slave = None
        self.process = None

    def start(self):
        """
        Start the shell process with a pseudo-terminal.

        Creates a PTY pair and spawns the shell process with the slave PTY
        connected to stdin/stdout/stderr.
        """
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
            shell_cmd = ["/bin/bash"]
            
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
        """
        Asynchronously read data from the PTY master.

        Args:
            size: Maximum number of bytes to read (default: 1024)

        Returns:
            bytes: Data read from the PTY, or empty bytes if EOF
        """
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
        """
        Write data to the PTY master (sends to shell stdin).

        Args:
            data: Bytes to write to the shell
        """
        if self.master is None:
            raise RuntimeError("PTY master is not initialized")
        os.write(self.master, data)

    def resize(self, rows: int, cols: int):
        if self.master is None:
            raise RuntimeError("PTY master is not initialized")
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)

    def stop(self):
        """
        Stop the shell process and clean up resources.

        Terminates the shell process gracefully and closes the PTY master.
        """
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
        """
        Check if the shell process is still running.

        Returns:
            bool: True if the process is running, False otherwise
        """
        return self.process is not None and self.process.poll() is None

    def fileno(self):
        """
        Get the file descriptor of the PTY master.

        Useful for select/poll operations.

        Returns:
            int: File descriptor number
        """
        return self.master

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures cleanup."""
        self.stop()
