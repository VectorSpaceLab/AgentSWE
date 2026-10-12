import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wirebatch import FrameParser, ProtocolError
from wirebatch.encode import encode_frame


class ParserTests(unittest.TestCase):
    def test_complete_frame(self):
        parser = FrameParser()
        frames = parser.feed(encode_frame(b"hello", request_id="r1"))
        self.assertEqual(frames[0]["payload"], b"hello")
        self.assertEqual(frames[0]["headers"]["x-request-id"], "r1")
        parser.finish()

    def test_two_complete_frames(self):
        parser = FrameParser()
        frames = parser.feed(encode_frame(b"a") + encode_frame(b"b"))
        self.assertEqual([frame["payload"] for frame in frames], [b"a", b"b"])

    def test_bad_length(self):
        with self.assertRaises(ProtocolError):
            FrameParser().feed(b"LEN -1\r\nContent-Type: x\r\n\r\n")


if __name__ == "__main__":
    unittest.main()
