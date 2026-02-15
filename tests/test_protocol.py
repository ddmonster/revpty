import random
import string
import unittest

from revpty.protocol.codec import encode, decode, ProtocolError
from revpty.protocol.frame import Frame, FrameType


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
