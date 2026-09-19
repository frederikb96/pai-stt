"""Round-trip checks for the voice socket's binary frame layout."""

import struct
import unittest

from pai_stt.framing import decode_down_frame, encode_up_frame


class TestUpFrame(unittest.TestCase):
    def test_header_is_two_big_endian_u32(self) -> None:
        frame = encode_up_frame(seq=7, sample_offset=1600, pcm=b"\x01\x02\x03\x04")
        seq, sample_offset = struct.unpack(">II", frame[:8])
        self.assertEqual(seq, 7)
        self.assertEqual(sample_offset, 1600)

    def test_payload_follows_header_unchanged(self) -> None:
        pcm = bytes(range(20))
        frame = encode_up_frame(seq=0, sample_offset=0, pcm=pcm)
        self.assertEqual(frame[8:], pcm)

    def test_empty_payload_is_a_bare_header(self) -> None:
        frame = encode_up_frame(seq=0, sample_offset=0, pcm=b"")
        self.assertEqual(len(frame), 8)


class TestDownFrame(unittest.TestCase):
    def test_ref_and_payload_round_trip(self) -> None:
        pcm = bytes(range(40))
        frame = struct.pack(">I", 42) + pcm
        ref, payload = decode_down_frame(frame)
        self.assertEqual(ref, 42)
        self.assertEqual(payload, pcm)

    def test_empty_payload(self) -> None:
        frame = struct.pack(">I", 0)
        ref, payload = decode_down_frame(frame)
        self.assertEqual(ref, 0)
        self.assertEqual(payload, b"")


if __name__ == "__main__":
    unittest.main()
