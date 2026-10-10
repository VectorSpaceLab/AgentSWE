import hashlib, unittest
from wirebatch import FrameParser, ProtocolError
from wirebatch.encode import encode_frame

class Tests(unittest.TestCase):
    def parse_chunks(self,wire,chunks,**kwargs):
        parser=FrameParser(**kwargs); output=[]
        for chunk in chunks: output.extend(parser.feed(chunk))
        parser.finish(); return output
    def assert_code(self,wire,code,**kwargs):
        with self.assertRaises(ProtocolError) as caught: FrameParser(**kwargs).feed(wire)
        self.assertEqual(caught.exception.code,code)
    def test_every_octet_boundary_multiple_binary_and_utf8(self):
        wire=encode_frame("héllo".encode(),content_type=" text/ü ",request_id="r",checksum=True)+encode_frame(b"\x00\xff")
        output=self.parse_chunks(wire,[wire[index:index+1] for index in range(len(wire))])
        self.assertEqual([frame["payload"] for frame in output],["héllo".encode(),b"\x00\xff"])
        self.assertEqual(output[0]["headers"]["content-type"],"text/ü")
    def test_split_after_headers_payload_and_terminator(self):
        wire=encode_frame(b"abcdef"); cut=wire.index(b"\r\n\r\n")+4
        chunks=[wire[:cut],wire[cut:cut+2],wire[cut+2:-1],wire[-1:]]
        self.assertEqual(self.parse_chunks(wire,chunks)[0]["payload"],b"abcdef")
    def test_length_header_and_encoding_codes(self):
        cases=[
            (b"BAD 0\r\nContent-Type: x\r\n\r\n\r\n","length_line"),
            (b"LEN +1\r\nContent-Type: x\r\n\r\na\r\n","length_value"),
            (b"LEN 0\r\nNoColon\r\n\r\n\r\n","header_syntax"),
            (b"LEN 0\r\nBad Name: x\r\nContent-Type: x\r\n\r\n\r\n","header_name"),
            (b"LEN 0\r\nX: \xff\r\nContent-Type: x\r\n\r\n\r\n","header_encoding"),
            (b"LEN 0\r\nContent-Type: x\r\ncontent-type: y\r\n\r\n\r\n","duplicate_header"),
            (b"LEN 0\r\n\r\n\r\n","missing_header"),
            (b"LEN 0\r\nContent-Type: \t \r\n\r\n\r\n","missing_header"),
        ]
        for wire,code in cases:
            with self.subTest(code=code): self.assert_code(wire,code)
    def test_checksum_terminator_and_no_partial_frame(self):
        digest=hashlib.sha256(b"abc").hexdigest()
        self.assert_code(f"LEN 3\r\nContent-Type: x\r\nChecksum-SHA256: {digest[:-1]}0\r\n\r\nabc\r\n".encode(),"checksum")
        self.assert_code(b"LEN 1\r\nContent-Type: x\r\n\r\naXX","frame_terminator")
        parser=FrameParser(); self.assertEqual(parser.feed(b"LEN 3\r\nContent-Type: x\r\n\r\na"),[])
        with self.assertRaises(ProtocolError) as caught: parser.finish()
        self.assertEqual(caught.exception.code,"truncated_frame")
    def test_payload_header_buffer_limits_and_configuration(self):
        self.assert_code(b"LEN 4\r\nContent-Type: x\r\n\r\n", "payload_limit", max_payload_bytes=3)
        self.assert_code(b"LEN 0\r\nX-Long: 1234567890\r\nContent-Type: x\r\n\r\n\r\n", "header_limit", max_header_bytes=20)
        with self.assertRaises(ProtocolError) as caught: FrameParser(max_payload_bytes=1,max_header_bytes=1).feed(b"x"*100)
        self.assertEqual(caught.exception.code,"buffer_limit")
        for args in ((0,10),(10,0),(True,10)):
            with self.assertRaises(ValueError): FrameParser(*args)
    def test_finish_at_boundary_and_case_insensitive_names(self):
        parser=FrameParser(); output=parser.feed(b"LEN 0\r\ncOnTeNt-TyPe: x\r\n\r\n\r\n"); parser.finish()
        self.assertEqual(output[0]["headers"],{"content-type":"x"})
if __name__ == "__main__": unittest.main(verbosity=2)
