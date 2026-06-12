"""Model banks: how each stream's values become arithmetic-coder symbols.

Integers are decomposed as a byte-length symbol (1..10) followed by that
many bytes, MSB first. Each (length, position) pair gets its own
adaptive byte model, so e.g. the high byte of 2-byte deltas and the low
byte of 2-byte deltas are modelled separately — that conditioning is
where most of the modelling power below the transform layer lives.

Encoder and decoder construct banks identically (lazily, in the same
order), so model state stays synchronised without transmission.
"""
from .arith import AdaptiveModel

MAX_INT_BYTES = 10  # covers zigzag of any |v| < 2^79; transforms bound at 2^62

# Per-stream bank layout, fixed by format v1 (FORMAT.md §6): nibble
# banks for the sparse per-row streams, byte banks for the four
# high-volume per-level streams. Measured justification in NOTES.md.
NIBBLE_INT_STREAMS = frozenset((
    "e_ms", "recv", "u_gap", "u_span", "len_b", "len_a",
    "bid_anchor", "ask_anchor",
))
NIBBLE_SIZE_STREAMS = frozenset()


class IntModels:
    """Codes non-negative integers (callers zigzag signed values first).

    Two bank layouts with identical conditional entropy but different
    adaptation cost (chain rule: H(hi|c) + H(lo|c,hi) = H(byte|c)):

    - byte mode: one 256-ary model per (length, position). Cheapest per
      symbol; right for high-volume streams where warm-up is amortised.
    - nibble mode: a 16-ary model per (length, position) for the high
      nibble, and one per (length, position, high) for the low nibble.
      A fresh context pays a <=16-symbol uniform prior instead of 256,
      which is what keeps sparse streams inside the 5% entropy gap
      under per-block model resets.
    """

    __slots__ = ("len_model", "sub_models", "nibble", "track")

    def __init__(self, track=False, nibble=False):
        self.len_model = AdaptiveModel(MAX_INT_BYTES, track=track)
        self.sub_models = {}
        self.nibble = nibble
        self.track = track

    def _model(self, key, alphabet):
        m = self.sub_models.get(key)
        if m is None:
            m = self.sub_models[key] = AdaptiveModel(alphabet,
                                                     track=self.track)
        return m

    def encode(self, enc, z):
        k = (z.bit_length() + 7) >> 3 or 1
        self.len_model.encode(enc, k - 1)
        for pos in range(k):
            b = (z >> ((k - 1 - pos) << 3)) & 0xFF
            if self.nibble:
                hi = b >> 4
                self._model((k, pos), 16).encode(enc, hi)
                self._model((k, pos, hi), 16).encode(enc, b & 15)
            else:
                self._model((k, pos), 256).encode(enc, b)

    def decode(self, dec):
        k = self.len_model.decode(dec) + 1
        z = 0
        for pos in range(k):
            if self.nibble:
                hi = self._model((k, pos), 16).decode(dec)
                b = (hi << 4) | self._model((k, pos, hi), 16).decode(dec)
            else:
                b = self._model((k, pos), 256).decode(dec)
            z = (z << 8) | b
        return z

    def models(self):
        yield self.len_model
        yield from self.sub_models.values()


class SizeModels:
    """Sizes: a zero/nonzero flag (37% zeros in the sample), then value-1."""

    __slots__ = ("flag", "val")

    def __init__(self, track=False, nibble=False):
        self.flag = AdaptiveModel(2, track=track)
        self.val = IntModels(track=track, nibble=nibble)

    def encode(self, enc, v):
        if v == 0:
            self.flag.encode(enc, 0)
        else:
            self.flag.encode(enc, 1)
            self.val.encode(enc, v - 1)

    def decode(self, dec):
        if self.flag.decode(dec) == 0:
            return 0
        return self.val.decode(dec) + 1

    def models(self):
        yield self.flag
        yield from self.val.models()


class EscapeModels:
    """Escape lines verbatim: byte length, then order-1 coded raw bytes.

    Raw bytes use the nibble split with the previous byte as context
    (escape traffic is sparse by design, so warm-up cost dominates).
    """

    __slots__ = ("length", "byte_ctx", "track")

    def __init__(self, track=False):
        self.length = IntModels(track=track, nibble=True)
        self.byte_ctx = {}
        self.track = track

    def _ctx(self, key):
        m = self.byte_ctx.get(key)
        if m is None:
            m = self.byte_ctx[key] = AdaptiveModel(16, track=self.track)
        return m

    def encode(self, enc, blob):
        self.length.encode(enc, len(blob))
        prev = 0
        for b in blob:
            hi = b >> 4
            self._ctx((prev,)).encode(enc, hi)
            self._ctx((prev, hi)).encode(enc, b & 15)
            prev = b

    def decode(self, dec):
        n = self.length.decode(dec)
        out = bytearray()
        prev = 0
        for _ in range(n):
            hi = self._ctx((prev,)).decode(dec)
            b = (hi << 4) | self._ctx((prev, hi)).decode(dec)
            out.append(b)
            prev = b
        return bytes(out)

    def models(self):
        yield from self.length.models()
        yield from self.byte_ctx.values()
