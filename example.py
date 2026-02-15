import errno
import os
import pty
import select
import subprocess
import sys
import time


def shell_with_pty_spawn(shell_cmd="/bin/bash"):
    """
    Example 1: Using pty.spawn() to run a shell
    This is the simplest way to get a PTY for a shell process
    """
    print("=== Example 1: Using pty.spawn() ===")

    def read_stdout(fd):
        """Callback for reading from the pseudo-terminal"""
        try:
            output = os.read(fd, 1024).decode()
            if output:
                print(f"Output: {repr(output)}")
        except OSError:
            pass

    # Spawn a shell in a pseudo-terminal
    pid, fd = pty.fork()

    if pid == 0:
        # Child process - run the shell
        os.execvp(shell_cmd, [shell_cmd])
    else:
        # Parent process - handle I/O
        print(f"Started shell with PID: {pid}, PTY fd: {fd}")

        # Write a command to the shell
        os.write(fd, b'echo "Hello from shell!"\n')
        os.write(fd, b"pwd\n")
        os.write(fd, b"exit\n")

        # Read output
        time.sleep(0.1)  # Give time for commands to execute

        try:
            while True:
                try:
                    output = os.read(fd, 4096).decode()
                    if output:
                        print(output, end="")
                    else:
                        break
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
        except Exception as e:
            print(f"Error reading: {e}")

        os.close(fd)
        _, status = os.waitpid(pid, 0)
        print(f"Shell exited with status: {status}")


def shell_with_popen():
    """
    Example 2: Using subprocess.Popen with PTY slave
    This gives more control over the process
    """
    print("\n=== Example 2: Using subprocess.Popen with PTY ===")

    # Create pseudo-terminal pair
    master_fd, slave_fd = pty.openpty()

    try:
        # Start shell with PTY slave as stdin/stdout/stderr
        proc = subprocess.Popen(
            ["/bin/bash"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )

        # Close slave fd in parent - child has its own copy
        os.close(slave_fd)

        print(f"Started shell with PID: {proc.pid}, PTY master fd: {master_fd}")

        # Send commands to shell
        commands = [
            b'export PS1="$ "\n',  # Set simple prompt
            b'echo "Hello from Popen!"\n',
            b"whoami\n",
            b"exit\n",
        ]

        for cmd in commands:
            os.write(master_fd, cmd)
            time.sleep(0.05)  # Small delay between commands

        # Read output with timeout
        output = b""
        start_time = time.time()
        timeout = 2

        while time.time() - start_time < timeout:
            # Check if there's data to read
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                    if chunk:
                        output += chunk
                    else:
                        break
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise

            # Check if process has ended
            if proc.poll() is not None:
                break

        print("Output from shell:")
        print(output.decode("utf-8", errors="replace"))

        # Wait for process to complete
        proc.wait()
        print(f"Shell exited with status: {proc.returncode}")

    finally:
        os.close(master_fd)


def interactive_shell_example():
    """
    Example 3: More interactive shell handling with proper prompt detection
    """
    print("\n=== Example 3: Interactive shell with prompt handling ===")

    master_fd, slave_fd = pty.openpty()

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "--norc", "--noprofile"],  # Clean shell environment
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)

        print(f"Started interactive shell with PID: {proc.pid}")

        # Set up non-blocking I/O
        import fcntl

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        commands = ['echo "Starting session"', "pwd", "ls -la", "exit"]

        for cmd in commands:
            # Send command
            os.write(master_fd, f"{cmd}\n".encode())
            print(f">>> {cmd}")

            # Read response with multiple attempts
            response = b""
            attempts = 0
            max_attempts = 10

            while attempts < max_attempts:
                try:
                    chunk = os.read(master_fd, 4096)
                    if chunk:
                        response += chunk
                    attempts += 1
                    time.sleep(0.05)
                except BlockingIOError:
                    attempts += 1
                    time.sleep(0.05)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise

            if response:
                print(response.decode("utf-8", errors="replace"))

        proc.wait(timeout=2)
        print(f"Shell exited with status: {proc.returncode}")

    finally:
        try:
            os.close(master_fd)
        except:
            pass


if __name__ == "__main__":
    # Run all examples
    shell_with_pty_spawn()
    shell_with_popen()
    interactive_shell_example()


def __main__():
    pid, fd = pty.fork()
    print("fork")
    os.write(fd, b"ls\n")
    rs = os.read(fd, 1024)
    print(rs)
