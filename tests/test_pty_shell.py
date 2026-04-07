"""Integration tests for PTYShell using real pseudo-terminals."""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest

from revpty.client.pty_shell import PTYShell


class PTYShellTests(unittest.IsolatedAsyncioTestCase):
    """Tests requiring real PTY - these run on Linux/macOS."""

    async def test_start_creates_process(self):
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            self.assertIsNotNone(pty.process)
            self.assertIsNotNone(pty.master)
            self.assertTrue(pty.is_running())
        finally:
            pty.stop()

    async def test_stop_terminates_process(self):
        pty = PTYShell("/bin/cat")
        pty.start()
        pty.stop()
        self.assertFalse(pty.is_running())
        self.assertIsNone(pty.process)
        self.assertIsNone(pty.master)

    async def test_write_and_read(self):
        """Write to PTY and read back the echo."""
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            # Give the process a moment to start
            await asyncio.sleep(0.1)

            # Write data
            pty.write(b"hello\n")

            # Read it back (cat echoes)
            data = await pty.read(size=1024, timeout=2)
            self.assertIsNotNone(data)
            self.assertIn(b"hello", data)
        finally:
            pty.stop()

    async def test_read_returns_empty_on_eof(self):
        """When process exits, read returns empty bytes."""
        pty = PTYShell("/bin/echo test")
        pty.start()
        try:
            # Wait for process to complete
            await asyncio.sleep(0.2)

            # Read until EOF
            chunks = []
            while True:
                data = await pty.read(timeout=1)
                if data is None or data == b"":
                    break
                chunks.append(data)

            # Process should have exited
            self.assertFalse(pty.is_running())
        finally:
            pty.stop()

    async def test_resize_does_not_crash(self):
        """resize() should work on a running PTY."""
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            await asyncio.sleep(0.1)
            # This should not raise
            pty.resize(rows=40, cols=120)
        finally:
            pty.stop()

    async def test_context_manager(self):
        """Using PTYShell as context manager should start/stop automatically."""
        with PTYShell("/bin/cat") as pty:
            self.assertTrue(pty.is_running())
            pty.write(b"test\n")
            data = await pty.read(timeout=1)
            self.assertIsNotNone(data)

        self.assertFalse(pty.is_running())

    async def test_read_before_start_raises(self):
        """read() before start() should raise RuntimeError."""
        pty = PTYShell()
        with self.assertRaises(RuntimeError):
            await pty.read()

    async def test_write_before_start_raises(self):
        """write() before start() should raise RuntimeError."""
        pty = PTYShell()
        with self.assertRaises(RuntimeError):
            pty.write(b"data")

    async def test_resize_before_start_raises(self):
        """resize() before start() should raise RuntimeError."""
        pty = PTYShell()
        with self.assertRaises(RuntimeError):
            pty.resize(24, 80)

    async def test_is_running_false_before_start(self):
        """is_running() should return False before start()."""
        pty = PTYShell()
        self.assertFalse(pty.is_running())

    async def test_fileno_returns_master(self):
        """fileno() returns the master fd after start."""
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            fd = pty.fileno()
            self.assertIsInstance(fd, int)
            self.assertGreater(fd, 2)  # Should be a real fd
        finally:
            pty.stop()

    async def test_start_with_shell_command(self):
        """PTYShell accepts shell command as string."""
        pty = PTYShell("/bin/sh -c 'echo hello'")
        pty.start()
        try:
            await asyncio.sleep(0.1)
            data = await pty.read(timeout=1)
            self.assertIsNotNone(data)
            self.assertIn(b"hello", data)
        finally:
            pty.stop()

    async def test_multiple_writes(self):
        """Multiple writes should work correctly."""
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            await asyncio.sleep(0.1)

            pty.write(b"first\n")
            await asyncio.sleep(0.1)
            data1 = await pty.read(timeout=1)

            pty.write(b"second\n")
            await asyncio.sleep(0.1)
            data2 = await pty.read(timeout=1)

            self.assertIsNotNone(data1)
            self.assertIsNotNone(data2)
        finally:
            pty.stop()

    async def test_default_shell(self):
        """PTYShell without args uses /bin/bash."""
        pty = PTYShell()
        self.assertEqual(pty.shell, "/bin/bash")

    async def test_stop_is_idempotent(self):
        """Calling stop() multiple times should be safe."""
        pty = PTYShell("/bin/cat")
        pty.start()
        pty.stop()
        pty.stop()  # Should not raise
        self.assertFalse(pty.is_running())

    async def test_read_timeout_returns_none(self):
        """read with timeout returns None if no data available."""
        pty = PTYShell("/bin/cat")
        pty.start()
        try:
            # Don't write anything, read should timeout
            data = await pty.read(size=1024, timeout=0.1)
            self.assertIsNone(data)
        finally:
            pty.stop()