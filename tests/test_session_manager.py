import unittest

from revpty.session.manager import SessionManager, SessionConfig, SessionState


class DummyPTY:
    def __init__(self, shell):
        self.shell = shell
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_starts_pty_for_client(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        session = await manager.attach("s1", "client", object())
        self.assertEqual(session.state, SessionState.RUNNING)
        self.assertTrue(session.pty.started)

    async def test_close_session_stops_pty(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        session = await manager.attach("s1", "client", object())
        await manager.close_session("s1")
        self.assertTrue(session.pty is None or session.pty.stopped)
