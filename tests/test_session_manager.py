import asyncio
import time
import unittest

from revpty.session.manager import SessionManager, Session, SessionConfig, SessionState


class DummyPTY:
    def __init__(self, shell):
        self.shell = shell
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeWS:
    def __init__(self, closed=False):
        self.closed = closed

    async def send_str(self, msg):
        pass


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, **kwargs):
        config = SessionConfig(**kwargs)
        return Session("s1", config)

    def test_get_peer_client_returns_browsers_and_viewers(self):
        s = self._make_session()
        b1, b2, v1 = FakeWS(), FakeWS(), FakeWS()
        s.browsers = {b1, b2}
        s.viewers = {v1}
        peers = s.get_peer("client")
        self.assertEqual(peers, {b1, b2, v1})

    def test_get_peer_browser_returns_clients(self):
        s = self._make_session()
        c1, c2 = FakeWS(), FakeWS()
        s.clients = {c1, c2}
        self.assertEqual(s.get_peer("browser"), {c1, c2})

    def test_get_peer_viewer_returns_clients(self):
        s = self._make_session()
        c1 = FakeWS()
        s.clients = {c1}
        self.assertEqual(s.get_peer("viewer"), {c1})

    def test_get_peer_unknown_returns_empty(self):
        s = self._make_session()
        self.assertEqual(s.get_peer("unknown"), set())

    def test_is_empty_true_when_no_connections(self):
        s = self._make_session()
        self.assertTrue(s.is_empty())

    def test_is_empty_false_with_connection(self):
        s = self._make_session()
        s.clients.add(FakeWS())
        self.assertFalse(s.is_empty())

    def test_is_idle_false_when_recent(self):
        s = self._make_session(idle_timeout=3600)
        s.last_active = time.time()
        self.assertFalse(s.is_idle())

    def test_is_idle_true_when_expired(self):
        s = self._make_session(idle_timeout=0)
        s.last_active = time.time() - 1
        self.assertTrue(s.is_idle())

    def test_attach_viewer(self):
        s = self._make_session()
        ws = FakeWS()
        s.attach("viewer", ws)
        self.assertIn(ws, s.viewers)

    def test_detach_client(self):
        s = self._make_session()
        ws = FakeWS()
        s.clients.add(ws)
        s.detach("client", ws)
        self.assertNotIn(ws, s.clients)

    def test_detach_browser(self):
        s = self._make_session()
        ws = FakeWS()
        s.browsers.add(ws)
        s.detach("browser", ws)
        self.assertNotIn(ws, s.browsers)

    def test_detach_viewer(self):
        s = self._make_session()
        ws = FakeWS()
        s.viewers.add(ws)
        s.detach("viewer", ws)
        self.assertNotIn(ws, s.viewers)

    def test_detach_nonexistent_ws_is_noop(self):
        s = self._make_session()
        s.detach("client", FakeWS())  # should not raise

    async def test_close_already_dead(self):
        s = self._make_session()
        s.state = SessionState.DEAD
        await s.close()
        self.assertEqual(s.state, SessionState.DEAD)

    async def test_close_clears_pty_and_buffer(self):
        s = self._make_session()
        pty = DummyPTY("/bin/bash")
        s.pty = pty
        s.state = SessionState.RUNNING
        s.output_buffer.append(b"data")
        await s.close()
        self.assertEqual(s.state, SessionState.DEAD)
        self.assertTrue(pty.stopped)
        self.assertIsNone(s.pty)
        self.assertEqual(len(s.output_buffer), 0)


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

    def test_get_existing_session(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        session = manager.get_or_create("s1")
        self.assertEqual(manager.get("s1"), session)

    def test_get_nonexistent_session(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        self.assertIsNone(manager.get("nope"))

    def test_detach(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        ws = FakeWS()
        manager.get_or_create("s1").clients.add(ws)
        manager.detach("s1", "client", ws)
        self.assertNotIn(ws, manager.get("s1").clients)

    def test_detach_nonexistent_session(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        manager.detach("nope", "client", FakeWS())  # should not raise

    async def test_cleanup_idle_removes_empty_idle_sessions(self):
        config = SessionConfig(idle_timeout=0)
        manager = SessionManager(lambda shell: DummyPTY(shell), config)
        session = manager.get_or_create("idle")
        session.last_active = time.time() - 1  # force idle
        await manager.cleanup_idle()
        self.assertIsNone(manager.get("idle"))

    async def test_cleanup_idle_keeps_active_sessions(self):
        config = SessionConfig(idle_timeout=3600)
        manager = SessionManager(lambda shell: DummyPTY(shell), config)
        session = manager.get_or_create("active")
        session.clients.add(FakeWS())
        await manager.cleanup_idle()
        self.assertIsNotNone(manager.get("active"))

    async def test_shutdown_closes_all_sessions(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        await manager.attach("s1", "client", FakeWS())
        await manager.attach("s2", "client", FakeWS())
        await manager.shutdown()
        self.assertEqual(len(manager.sessions), 0)

    async def test_start_cleanup_task(self):
        manager = SessionManager(lambda shell: DummyPTY(shell), SessionConfig())
        await manager.start_cleanup_task(interval=1)
        self.assertIsNotNone(manager._cleanup_task)
        await manager.shutdown()
