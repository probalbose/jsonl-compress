"""Per-transform invertibility tests, each transform in isolation."""
import random
import unittest

from jsonlz.transforms import (Schema, extract_candidate, infer_qexp,
                               token_to_v8, ts_to_us, unzigzag, us_to_iso,
                               v8_to_token, zigzag)

SAMPLE_LINE = (
    b'{"stream":"orderbook_depth","symbol":"BTCUSDT",'
    b'"event_time":"2026-06-11T00:00:00.014000+00:00",'
    b'"received_at":"2026-06-11T00:00:00.078676+00:00",'
    b'"payload":{"e":"depthUpdate","E":1781136000014,"s":"BTCUSDT",'
    b'"U":95576848412,"u":95576848489,'
    b'"b":[["61510.99000000","1.41173000"],["61510.98000000","0.00071000"]],'
    b'"a":[["61511.00000000","1.85835000"]]}}\n')


class TestZigzag(unittest.TestCase):
    def test_roundtrip(self):
        for v in [0, 1, -1, 2, -2, 10**18, -10**18, (1 << 61), -(1 << 61)]:
            self.assertEqual(unzigzag(zigzag(v)), v)

    def test_ordering(self):
        self.assertEqual([zigzag(v) for v in (0, -1, 1, -2, 2)],
                         [0, 1, 2, 3, 4])


class TestTimestamps(unittest.TestCase):
    def test_known_value(self):
        s = "2026-06-11T00:00:00.014000+00:00"
        us = ts_to_us(s)
        self.assertEqual(us, 1781136000014000)
        self.assertEqual(us_to_iso(us), s)

    def test_fuzz_roundtrip(self):
        rng = random.Random(0)
        for _ in range(2000):
            us = rng.randrange(0, 4 * 10**15)  # ~year 2096
            self.assertEqual(ts_to_us(us_to_iso(us)), us)

    def test_subsecond_edges(self):
        for us in (0, 1, 999999, 10**6, 10**6 - 1):
            self.assertEqual(ts_to_us(us_to_iso(us)), us)


class TestNumericTokens(unittest.TestCase):
    def test_roundtrip_examples(self):
        for tok in ("61510.99000000", "0.00000000", "0.00071000",
                    "100000.00000000", "1.41173000"):
            v8 = token_to_v8(tok)
            self.assertIsNotNone(v8, tok)
            self.assertEqual(v8_to_token(v8), tok)

    def test_fuzz_roundtrip(self):
        rng = random.Random(1)
        for _ in range(5000):
            v8 = rng.randrange(0, 10**14)
            self.assertEqual(token_to_v8(v8_to_token(v8)), v8)

    def test_rejects_non_canonical(self):
        for tok in ("01.00000000", "1.0", "1.000000000", "-1.00000000",
                    "1e5", "1.0000000", ".00000000", "1.", "", "abc",
                    "1,00000000", "+1.00000000", " 1.00000000"):
            self.assertIsNone(token_to_v8(tok), tok)


class TestQuantumInference(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(infer_qexp([6151099000000]), 6)        # 0.01 tick
        self.assertEqual(infer_qexp([52340000, 52350000]), 4)   # 0.0001 tick
        self.assertEqual(infer_qexp([100, 1000]), 2)
        self.assertEqual(infer_qexp([7]), 0)
        self.assertEqual(infer_qexp([]), 8)
        self.assertEqual(infer_qexp([0, 0]), 8)                 # zeros divide all
        self.assertEqual(infer_qexp([10**8, 3]), 0)


class TestRowExtractRender(unittest.TestCase):
    def schema(self):
        return Schema(("orderbook_depth", "BTCUSDT", "depthUpdate", "BTCUSDT"),
                      pexp=6, sexp=3)

    def test_sample_line_roundtrip(self):
        cand = extract_candidate(SAMPLE_LINE)
        self.assertIsNotNone(cand)
        schema = self.schema()
        row = schema.quantize(cand)
        self.assertIsNotNone(row)
        self.assertEqual(schema.render_row(*row), SAMPLE_LINE)

    def test_escape_cases(self):
        base = SAMPLE_LINE
        cases = {
            "no newline": base[:-1],
            "blank": b"\n",
            "malformed json": b'{"stream":\n',
            "glued records": base[:-1] + base,
            "extra key": base.replace(b'"u":95576848489',
                                      b'"u":95576848489,"x":1'),
            "non-monotonic bids": base.replace(
                b'["61510.98000000"', b'["61511.99000000"'),
            "float E": base.replace(b'"E":1781136000014',
                                    b'"E":1781136000014.0'),
            "event_time mismatch": base.replace(
                b'"event_time":"2026-06-11T00:00:00.014000+00:00"',
                b'"event_time":"2026-06-11T00:00:00.015000+00:00"'),
        }
        for name, line in cases.items():
            self.assertIsNone(extract_candidate(line), name)

    def test_whitespace_variant_caught_by_render_equality(self):
        # Cosmetic deviations parse fine structurally; the encoder's
        # render-equality check is what sends them to the escape channel.
        line = SAMPLE_LINE.replace(b'"u":95576848489', b'"u": 95576848489')
        cand = extract_candidate(line)
        self.assertIsNotNone(cand)
        schema = self.schema()
        row = schema.quantize(cand)
        self.assertNotEqual(schema.render_row(*row), line)
        self.assertEqual(schema.render_row(*row), SAMPLE_LINE)

    def test_quantize_rejects_off_quantum(self):
        cand = extract_candidate(SAMPLE_LINE)
        coarse = Schema(cand.constants, pexp=7, sexp=3)  # 0.1 tick: too coarse
        self.assertIsNone(coarse.quantize(cand))
        wrong_const = Schema(("x", "BTCUSDT", "depthUpdate", "BTCUSDT"), 6, 3)
        self.assertIsNone(wrong_const.quantize(cand))

    def test_constants_with_json_escapes(self):
        schema = Schema(('order"book\\', "BTCé", "dep\tth", "s"), 6, 0)
        line = schema.render_row(1781136000014, 1781136000078676,
                                 5, 9, [(6151099, 141173000)], [])
        cand = extract_candidate(line)
        self.assertIsNotNone(cand)
        row = schema.quantize(cand)
        self.assertEqual(schema.render_row(*row), line)

    def test_empty_sides(self):
        schema = self.schema()
        line = schema.render_row(1781136000014, 1781136000078676, 5, 9, [], [])
        cand = extract_candidate(line)
        row = schema.quantize(cand)
        self.assertEqual(schema.render_row(*row), line)

    def test_fuzz_render_extract_roundtrip(self):
        rng = random.Random(2)
        schema = self.schema()
        for _ in range(300):
            E = rng.randrange(1, 4 * 10**12)
            recv = rng.randrange(0, 4 * 10**15)
            U = rng.randrange(0, 10**12)
            u = U + rng.randrange(0, 10**4)
            ticks = sorted(rng.sample(range(1, 10**7), rng.randrange(0, 6)),
                           reverse=True)
            bids = [(t, rng.randrange(0, 10**9)) for t in ticks]
            ticks = sorted(rng.sample(range(1, 10**7), rng.randrange(0, 6)))
            asks = [(t, rng.randrange(0, 10**9)) for t in ticks]
            line = schema.render_row(E, recv, U, u, bids, asks)
            cand = extract_candidate(line)
            self.assertIsNotNone(cand)
            row = schema.quantize(cand)
            self.assertEqual(row, (E, recv, U, u, bids, asks))
            self.assertEqual(schema.render_row(*row), line)


if __name__ == "__main__":
    unittest.main()
