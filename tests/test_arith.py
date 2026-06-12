"""Unit tests for the arithmetic coder and adaptive model."""
import math
import random
import unittest

from jsonlz.arith import AdaptiveModel, Decoder, Encoder, MAX_TOTAL


def roundtrip(symbols, alphabet, increment=24):
    enc = Encoder()
    m = AdaptiveModel(alphabet, increment=increment)
    for s in symbols:
        m.encode(enc, s)
    data = enc.finish()
    dec = Decoder(data)
    m2 = AdaptiveModel(alphabet, increment=increment)
    out = [m2.decode(dec) for _ in symbols]
    return out, data


def empirical_entropy_bits(symbols):
    counts = {}
    for s in symbols:
        counts[s] = counts.get(s, 0) + 1
    n = len(symbols)
    return -sum(c / n * math.log2(c / n) for c in counts.values()) * n


class TestRoundTrip(unittest.TestCase):
    def test_empty(self):
        out, data = roundtrip([], 256)
        self.assertEqual(out, [])
        self.assertLessEqual(len(data), 2)

    def test_single_symbol(self):
        out, _ = roundtrip([7], 256)
        self.assertEqual(out, [7])

    def test_alphabet_edges(self):
        for alphabet in (2, 3, 9, 10, 255, 256):
            syms = [0, alphabet - 1] * 50
            out, _ = roundtrip(syms, alphabet)
            self.assertEqual(out, syms, f"alphabet={alphabet}")

    def test_random_streams(self):
        for seed in range(10):
            rng = random.Random(seed)
            alphabet = rng.choice([2, 9, 64, 256])
            n = rng.choice([1, 10, 1000, 20000])
            syms = [rng.randrange(alphabet) for _ in range(n)]
            out, _ = roundtrip(syms, alphabet)
            self.assertEqual(out, syms, f"seed={seed}")

    def test_skewed_stream_with_rescale(self):
        rng = random.Random(42)
        # geometric-ish: forces many rescales (total caps at 2^16)
        syms = [min(int(rng.expovariate(0.7)), 255) for _ in range(50000)]
        out, _ = roundtrip(syms, 256)
        self.assertEqual(out, syms)

    def test_nonstationary(self):
        rng = random.Random(1)
        syms = [rng.randrange(0, 8) for _ in range(5000)] + \
               [rng.randrange(120, 128) for _ in range(5000)]
        out, _ = roundtrip(syms, 256)
        self.assertEqual(out, syms)

    def test_multiple_models_one_stream(self):
        # interleave two models over one coder, as the codec does
        rng = random.Random(3)
        flags = [rng.random() < 0.3 for _ in range(2000)]
        vals = [rng.randrange(256) for _ in range(2000)]
        enc = Encoder()
        mf, mv = AdaptiveModel(2), AdaptiveModel(256)
        for f, v in zip(flags, vals):
            mf.encode(enc, int(f))
            if f:
                mv.encode(enc, v)
        data = enc.finish()
        dec = Decoder(data)
        mf2, mv2 = AdaptiveModel(2), AdaptiveModel(256)
        for f, v in zip(flags, vals):
            self.assertEqual(mf2.decode(dec), int(f))
            if f:
                self.assertEqual(mv2.decode(dec), v)


class TestRate(unittest.TestCase):
    def assert_rate_within(self, syms, alphabet, tolerance):
        _, data = roundtrip(syms, alphabet)
        h_bits = empirical_entropy_bits(syms)
        coded_bits = len(data) * 8
        # alphabet-size bits of slack for model warm-up and flush
        slack = alphabet + 64
        self.assertLessEqual(
            coded_bits, h_bits * (1 + tolerance) + slack,
            f"coded {coded_bits} vs entropy {h_bits:.0f} bits")

    def test_rate_skewed_256(self):
        rng = random.Random(7)
        syms = [min(int(rng.expovariate(0.5)), 255) for _ in range(200000)]
        self.assert_rate_within(syms, 256, 0.05)

    def test_rate_binary_skewed(self):
        rng = random.Random(8)
        syms = [int(rng.random() < 0.05) for _ in range(200000)]
        self.assert_rate_within(syms, 2, 0.05)

    def test_rate_near_uniform(self):
        rng = random.Random(9)
        syms = [rng.randrange(200) for _ in range(200000)]
        self.assert_rate_within(syms, 256, 0.05)

    def test_degenerate_single_symbol(self):
        syms = [42] * 100000
        _, data = roundtrip(syms, 256)
        # constant stream must cost far below 1 bit/symbol
        self.assertLess(len(data) * 8, 0.02 * len(syms))


class TestModel(unittest.TestCase):
    def test_fenwick_matches_naive(self):
        rng = random.Random(5)
        n = 37
        m = AdaptiveModel(n, increment=24)
        naive = [1] * n
        for _ in range(3000):
            s = rng.randrange(n)
            lo = m._cum(s)
            self.assertEqual(lo, sum(naive[:s]))
            self.assertEqual(m._cum(s + 1) - lo, naive[s])
            found, flo = m._find(lo)
            self.assertEqual((found, flo), (s, lo))
            m._bump(s)
            naive[s] += 24
            if sum(naive) > MAX_TOTAL:
                naive = [(f + 1) >> 1 for f in naive]
            self.assertEqual(m.total, sum(naive))

    def test_all_symbols_codable_after_rescales(self):
        m = AdaptiveModel(256)
        for _ in range(10000):
            m._bump(0)  # hammer one symbol through many rescales
        for s in range(256):
            self.assertGreaterEqual(m._cum(s + 1) - m._cum(s), 1)

    def test_histogram_tracking(self):
        m = AdaptiveModel(4, track=True)
        enc = Encoder()
        for s in [0, 1, 1, 3]:
            m.encode(enc, s)
        self.assertEqual(m.hist, [1, 2, 0, 1])


if __name__ == "__main__":
    unittest.main()
