"""End-to-end round-trip tests: adversarial inputs and property-based fuzz."""
import os
import random
import tempfile
import unittest

from jsonlz.decoder import decompress
from jsonlz.encoder import compress
from jsonlz.format import CorruptBlockError, FormatError
from jsonlz.transforms import Schema, us_to_iso


def make_line(E, recv_us, U, u, bids, asks,
              stream="orderbook_depth", symbol="BTCUSDT",
              e="depthUpdate", s="BTCUSDT"):
    return Schema((stream, symbol, e, s), 0, 0).render_row(
        E, recv_us, U, u, bids, asks)


def synth_rows(rng, n, tick_exp=6, size_exp=3, symbol="BTCUSDT"):
    """Sample-like rows: near-monotonic E with occasional regressions,
    sorted books around a wandering mid, sizes with many zeros."""
    out = []
    E = 1781136000000
    U = 95576848412
    mid = 6151099
    for _ in range(n):
        E += rng.choice([100, 100, 200, -100, 300, 101])
        recv = E * 1000 + rng.randrange(50000, 400000)
        span = rng.randrange(1, 60)
        U += rng.choice([1, 1, 1, rng.randrange(-2000, 2000)])
        u = U + span
        mid += rng.randrange(-300, 300)
        nb, na = rng.randrange(0, 12), rng.randrange(0, 12)
        bticks = sorted(rng.sample(range(max(mid - 5000, 1), mid), nb) if nb else [],
                        reverse=True)
        aticks = sorted(rng.sample(range(mid + 1, mid + 5000), na) if na else [])

        def size():
            return 0 if rng.random() < 0.4 else rng.randrange(1, 10**5)

        q = 10 ** tick_exp
        sq = 10 ** size_exp
        line = Schema(("orderbook_depth", symbol, "depthUpdate", symbol),
                      tick_exp, size_exp).render_row(
            E, recv, U, u + 0,
            [(t, size()) for t in bticks], [(t, size()) for t in aticks])
        out.append(line)
        U = u
    return out


class RoundTripCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def roundtrip(self, data, block_lines=64):
        src, comp, rest = (self.path(n) for n in ("src", "comp", "rest"))
        with open(src, "wb") as f:
            f.write(data)
        compress(src, comp, block_lines=block_lines)
        decompress(comp, rest)
        with open(rest, "rb") as f:
            restored = f.read()
        self.assertEqual(restored, data)
        return os.path.getsize(comp)


class TestAdversarial(RoundTripCase):
    def test_empty_file(self):
        self.roundtrip(b"")

    def test_single_line(self):
        self.roundtrip(synth_rows(random.Random(0), 1)[0])

    def test_single_line_no_newline(self):
        self.roundtrip(b'{"stream":"x"}')

    def test_malformed_json_line(self):
        rows = synth_rows(random.Random(1), 10)
        rows[4] = b'{"stream":"orderbook_depth", BROKEN\n'
        self.roundtrip(b"".join(rows))

    def test_unseen_key(self):
        rows = synth_rows(random.Random(2), 10)
        rows[3] = rows[3].replace(b',"payload":', b',"extra":1,"payload":')
        self.roundtrip(b"".join(rows))

    def test_non_monotonic_timestamp(self):
        rng = random.Random(3)
        rows = synth_rows(rng, 50)  # generator already inserts regressions
        self.roundtrip(b"".join(rows))

    def test_blank_lines_and_glued_records(self):
        rows = synth_rows(random.Random(4), 12)
        rows[5] = rows[5].rstrip(b"\n") + rows[6]      # glued, like the sample
        rows[6] = b"\n"
        self.roundtrip(b"".join(rows))

    def test_all_escape_no_schema(self):
        self.roundtrip(b"not json\n" * 100)

    def test_mixed_symbols_second_escapes(self):
        a = synth_rows(random.Random(5), 20, symbol="BTCUSDT")
        b = synth_rows(random.Random(6), 20, symbol="ETHUSDT")
        self.roundtrip(b"".join(x for pair in zip(a, b) for x in pair))

    def test_finer_tick_after_first_block(self):
        # first block infers a coarse quantum; later finer-grained rows
        # must escape, not corrupt
        coarse = synth_rows(random.Random(7), 64, tick_exp=6)
        fine = synth_rows(random.Random(8), 10, tick_exp=2)
        self.roundtrip(b"".join(coarse + fine), block_lines=64)

    def test_no_trailing_newline_at_eof(self):
        rows = synth_rows(random.Random(9), 8)
        self.roundtrip(b"".join(rows)[:-1])

    def test_crlf_goes_to_escape(self):
        rows = synth_rows(random.Random(10), 8)
        rows[2] = rows[2][:-1] + b"\r\n"
        self.roundtrip(b"".join(rows))

    def test_huge_numbers_escape(self):
        rows = synth_rows(random.Random(11), 8)
        rows[1] = rows[1].replace(b'"U":', b'"U":9' + b"9" * 30 + b',"X":',
                                  1).replace(b',"X":', b"", 1)
        self.roundtrip(b"".join(rows))


class TestCorruptionDetection(RoundTripCase):
    def test_flipped_byte_detected(self):
        src, comp, rest = (self.path(n) for n in ("src", "comp", "rest"))
        with open(src, "wb") as f:
            f.write(b"".join(synth_rows(random.Random(12), 200)))
        compress(src, comp, block_lines=64)
        with open(comp, "rb") as f:
            blob = bytearray(f.read())
        blob[len(blob) // 2] ^= 0x40
        with open(comp, "wb") as f:
            f.write(blob)
        with self.assertRaises(CorruptBlockError):
            decompress(comp, rest)

    def test_corrupt_header_detected(self):
        src, comp, rest = (self.path(n) for n in ("src", "comp", "rest"))
        with open(src, "wb") as f:
            f.write(b"".join(synth_rows(random.Random(14), 50)))
        compress(src, comp)
        with open(comp, "rb") as f:
            blob = bytearray(f.read())
        blob[20] ^= 0x01  # inside the header JSON (pexp/sexp region)
        with open(comp, "wb") as f:
            f.write(blob)
        with self.assertRaises(FormatError):
            decompress(comp, rest)

    def test_truncation_detected(self):
        src, comp, rest = (self.path(n) for n in ("src", "comp", "rest"))
        with open(src, "wb") as f:
            f.write(b"".join(synth_rows(random.Random(13), 100)))
        compress(src, comp)
        with open(comp, "rb") as f:
            blob = f.read()
        with open(comp, "wb") as f:
            f.write(blob[:-30])
        with self.assertRaises(FormatError):
            decompress(comp, rest)


class TestPropertyBased(RoundTripCase):
    def test_random_jsonl_with_corruptions(self):
        for seed in range(8):
            rng = random.Random(100 + seed)
            rows = synth_rows(rng, rng.randrange(1, 400),
                              tick_exp=rng.choice([2, 4, 6]),
                              size_exp=rng.choice([0, 3, 5]))
            # sprinkle corruptions
            for _ in range(rng.randrange(0, 6)):
                i = rng.randrange(len(rows))
                kind = rng.randrange(5)
                if kind == 0:
                    rows[i] = b"\n"
                elif kind == 1:
                    rows[i] = rows[i][:rng.randrange(len(rows[i]))] + b"\n"
                elif kind == 2:
                    rows[i] = rows[i].rstrip(b"\n") + rows[
                        rng.randrange(len(rows))]
                elif kind == 3:
                    rows[i] = rows[i].replace(b'"E":', b'"E": ', 1)
                else:
                    rows[i] = bytes(rng.randrange(32, 127)
                                    for _ in range(rng.randrange(0, 50))) + b"\n"
            self.roundtrip(b"".join(rows),
                           block_lines=rng.choice([16, 64, 4096]))

    def test_compresses_conformant_data(self):
        rows = synth_rows(random.Random(999), 2000)
        raw = b"".join(rows)
        size = self.roundtrip(raw, block_lines=1024)
        self.assertLess(size, len(raw) // 4,
                        f"only {len(raw)} -> {size}")


if __name__ == "__main__":
    unittest.main()
