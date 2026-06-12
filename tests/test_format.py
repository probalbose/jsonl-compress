"""Unit tests for container primitives: CRC32, varints, header framing."""
import io
import random
import unittest
import zlib  # test-only cross-check; never imported by the codec

from jsonlz.format import (ByteReader, FormatError, append_uvarint, crc32,
                           read_header, read_uvarint_stream, write_header)
from jsonlz.transforms import Schema


class TestCRC32(unittest.TestCase):
    def test_known_vector(self):
        # IEEE 802.3 check value
        self.assertEqual(crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(crc32(b""), 0)

    def test_matches_zlib(self):
        rng = random.Random(0)
        for _ in range(50):
            blob = bytes(rng.randrange(256)
                         for _ in range(rng.randrange(0, 300)))
            self.assertEqual(crc32(blob), zlib.crc32(blob))


class TestUvarint(unittest.TestCase):
    def test_roundtrip(self):
        values = [0, 1, 127, 128, 300, 2**21, 2**62, 2**70 - 1]
        buf = bytearray()
        for v in values:
            append_uvarint(buf, v)
        r = ByteReader(bytes(buf))
        self.assertEqual([r.uvarint() for _ in values], values)
        self.assertEqual(r.pos, len(buf))
        stream = io.BytesIO(bytes(buf))
        self.assertEqual([read_uvarint_stream(stream) for _ in values],
                         values)

    def test_truncated(self):
        with self.assertRaises(FormatError):
            ByteReader(b"\x80").uvarint()
        with self.assertRaises(FormatError):
            read_uvarint_stream(io.BytesIO(b"\x80"))


class TestHeader(unittest.TestCase):
    def test_roundtrip(self):
        schema = Schema(("orderbook_depth", "BTCUSDT",
                         "depthUpdate", "BTCUSDT"), 6, 3)
        buf = io.BytesIO()
        write_header(buf, schema)
        buf.seek(0)
        meta = read_header(buf)
        self.assertEqual(meta["schema"]["constants"][1], "BTCUSDT")
        self.assertEqual(meta["schema"]["pexp"], 6)

    def test_none_schema(self):
        buf = io.BytesIO()
        write_header(buf, None)
        buf.seek(0)
        self.assertIsNone(read_header(buf)["schema"])

    def test_corruption_detected_everywhere(self):
        buf = io.BytesIO()
        write_header(buf, Schema(("s", "x", "e", "x"), 6, 3))
        blob = bytearray(buf.getvalue())
        for i in range(len(blob)):
            broken = bytearray(blob)
            broken[i] ^= 0x10
            with self.assertRaises(FormatError, msg=f"byte {i}"):
                read_header(io.BytesIO(bytes(broken)))


if __name__ == "__main__":
    unittest.main()
