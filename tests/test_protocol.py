import base64
import json
import random
import string
import time
import unittest

from revpty.protocol.codec import encode, decode, ProtocolError
from revpty.protocol.frame import Frame, FrameType, Role, PROTOCOL_VERSION


class FrameValidationTests(unittest.TestCase):
    """Tests for Frame.validate() covering all FrameType branches."""

    def _check_invalid(self, frame):
        ok, err = frame.validate()
        self.assertFalse(ok)
        self.assertIsNotNone(err)

    def _check_valid(self, frame):
        ok, err = frame.validate()
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_valid_input_frame(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.INPUT.value, data=b"x"))

    def test_input_requires_data(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.INPUT.value))

    def test_input_no_rows_cols(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.INPUT.value, data=b"x", rows=24))

    def test_output_requires_data(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.OUTPUT.value))

    def test_output_no_rows_cols(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.OUTPUT.value, data=b"x", rows=24))

    def test_resize_requires_rows_and_cols(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.RESIZE.value))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.RESIZE.value, rows=24))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.RESIZE.value, cols=80))

    def test_resize_valid(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.RESIZE.value, rows=24, cols=80))

    def test_resize_no_data(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.RESIZE.value, rows=24, cols=80, data=b"x"))

    def test_attach_no_data_no_rows_cols(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.ATTACH.value))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.ATTACH.value, data=b"x"))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.ATTACH.value, rows=24))

    def test_detach_no_data_no_rows_cols(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.DETACH.value))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.DETACH.value, data=b"x"))

    def test_ping_no_data_no_rows_cols(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.PING.value))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.PING.value, data=b"x"))
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.PING.value, rows=24))

    def test_pong_no_data_no_rows_cols(self):
        self._check_valid(Frame(session="s1", role="server", type=FrameType.PONG.value))
        self._check_invalid(Frame(session="s1", role="server", type=FrameType.PONG.value, data=b"x"))

    def test_status_no_rows_cols(self):
        self._check_valid(Frame(session="s1", role="server", type=FrameType.STATUS.value, data=b"{}"))
        self._check_invalid(Frame(session="s1", role="server", type=FrameType.STATUS.value, data=b"{}", rows=24))

    def test_file_requires_data(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.FILE.value))

    def test_file_valid(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.FILE.value, data=b"{}"))

    def test_control_requires_data(self):
        self._check_invalid(Frame(session="s1", role="client", type=FrameType.CONTROL.value))

    def test_control_valid(self):
        self._check_valid(Frame(session="s1", role="client", type=FrameType.CONTROL.value, data=b"{}"))

    def test_invalid_role(self):
        self._check_invalid(Frame(session="s1", role="invalid_role", type=FrameType.PING.value))

    def test_invalid_protocol_version(self):
        self._check_invalid(Frame(v=0, session="s1", role="client", type=FrameType.PING.value))


class CodecTests(unittest.TestCase):
    """Tests for encode/decode error paths and edge cases."""

    def test_decode_invalid_json(self):
        with self.assertRaises(ProtocolError):
            decode("not json at all")

    def test_decode_missing_fields(self):
        with self.assertRaises(ProtocolError) as ctx:
            decode('{"v": 1}')
        self.assertIn("missing", str(ctx.exception).lower())

    def test_decode_bad_base64(self):
        obj = json.dumps({
            "v": PROTOCOL_VERSION, "session": "s1",
            "role": "client", "type": "input", "data": "!!invalid!!"
        })
        with self.assertRaises(ProtocolError):
            decode(obj)

    def test_encode_resize_roundtrip(self):
        frame = Frame(session="s1", role="client", type=FrameType.RESIZE.value, rows=24, cols=80)
        raw = encode(frame)
        decoded = decode(raw)
        self.assertEqual(decoded.rows, 24)
        self.assertEqual(decoded.cols, 80)
        self.assertIsNone(decoded.data)

    def test_encode_auto_fills_timestamp(self):
        frame = Frame(session="s1", role="client", type=FrameType.PING.value)
        self.assertIsNone(frame.ts)
        encode(frame)
        self.assertIsNotNone(frame.ts)
        self.assertGreaterEqual(frame.ts, time.time() - 1)

    def test_encode_decode_bytes_preserved(self):
        frame = Frame(session="s1", role="client", type=FrameType.INPUT.value, data=b"\x00\xff\x01")
        decoded = decode(encode(frame))
        self.assertEqual(decoded.data, b"\x00\xff\x01")


class ProtocolTests(unittest.TestCase):
    def test_encode_decode_input(self):
        frame = Frame(session="s1", role="client", type=FrameType.INPUT.value, data=b"ls\n")
        raw = encode(frame)
        decoded = decode(raw)
        self.assertEqual(decoded.type, FrameType.INPUT.value)
        self.assertEqual(decoded.data, b"ls\n")

    def test_encode_decode_output(self):
        frame = Frame(session="s1", role="client", type=FrameType.OUTPUT.value, data=b"ok")
        raw = encode(frame)
        decoded = decode(raw)
        self.assertEqual(decoded.type, FrameType.OUTPUT.value)
        self.assertEqual(decoded.data, b"ok")

    def test_encode_decode_status(self):
        frame = Frame(session="s1", role="browser", type=FrameType.STATUS.value, data=b'{"peers":0}')
        raw = encode(frame)
        decoded = decode(raw)
        self.assertEqual(decoded.type, FrameType.STATUS.value)
        self.assertEqual(decoded.data, b'{"peers":0}')

    def test_invalid_frame_type(self):
        raw = encode(Frame(session="s1", role="client", type="stdin", data=b"bad"))
        with self.assertRaises(ProtocolError):
            decode(raw)

    def test_fuzz_decode_invalid(self):
        for _ in range(100):
            raw = "".join(random.choice(string.printable) for _ in range(64))
            try:
                decode(raw)
            except ProtocolError:
                continue
