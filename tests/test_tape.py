"""The tape: websocket framing and the per-second features."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import tape  # noqa: E402


def unmask(frame: bytes):
    b1, b2 = frame[0], frame[1]
    ln, i = b2 & 0x7F, 2
    if ln == 126:
        ln = struct.unpack(">H", frame[2:4])[0]; i = 4
    elif ln == 127:
        ln = struct.unpack(">Q", frame[2:10])[0]; i = 10
    mask = frame[i:i + 4]
    data = frame[i + 4:i + 4 + ln]
    return b1 & 0x0F, bytes(c ^ mask[k % 4] for k, c in enumerate(data))


class Frames(unittest.TestCase):
    def test_round_trip_small_medium_large(self):
        for n in (5, 300, 70000):
            op, data = unmask(tape.encode_frame(b"x" * n))
            self.assertEqual((op, len(data)), (1, n))

    def test_server_frames_are_read(self):
        ws = tape.WS.__new__(tape.WS)
        ws.sock = None
        ws.buf = bytes([0x81, 5]) + b"hello" + bytes([0x01, 2]) + b"ab" + bytes([0x80, 2]) + b"cd"
        self.assertEqual(ws.recv(), "hello")
        self.assertEqual(ws.recv(), "abcd", "fragments join")


class Features(unittest.TestCase):
    def setUp(self):
        reset()

    def test_buying_pressure_reads_positive(self):
        b = tape.Book("X")
        t0 = 1000.0
        b.update({"lp": "100", "v": "0", "tbq": "5000", "tsq": "5000"}, t0)
        for i in range(1, 200):
            b.update({"lp": str(100 + i * 0.01), "v": str(i * 100), "tbq": "9000", "tsq": "3000"}, t0 + i)
        f = b.features(t0 + 199)
        self.assertGreater(f["chg_1m"], 0)
        self.assertGreater(f["upticks"], 0.9)
        self.assertEqual(f["imbalance"], 0.75)
        self.assertGreater(f["momentum"], 0.5)

    def test_selling_reads_negative_and_ignores_bad_fields(self):
        b = tape.Book("X")
        b.update({"lp": "100", "v": "10"}, 0)
        for i in range(1, 120):
            b.update({"lp": str(100 - i * 0.02), "v": str(10 + i * 50), "tbq": "1000", "tsq": "x"}, i)
        f = b.features(119)
        self.assertLess(f["momentum"], -0.3)
        self.assertIsNone(f["imbalance"])

    def test_no_price_no_features(self):
        self.assertIsNone(tape.Book("X").features(10))

    def test_stale_live_file_is_ignored(self):
        import json
        import time
        tape.TAPE_DIR.mkdir(parents=True, exist_ok=True)
        tape.LIVE_FILE.write_text(json.dumps({"symbols": {"A": {"updated": time.time()}, "B": {"updated": 1}}}))
        self.assertEqual(list(tape.read_live()), ["A"])


if __name__ == "__main__":
    unittest.main()
