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


class IntModels:
    """Codes non-negative integers (callers zigzag signed values first)."""

    __slots__ = ("len_model", "byte_models", "track")

    def __init__(self, track=False):
        self.len_model = AdaptiveModel(MAX_INT_BYTES, track=track)
        self.byte_models = {}
        self.track = track

    def _byte_model(self, k, pos):
        m = self.byte_models.get((k, pos))
        if m is None:
            m = self.byte_models[(k, pos)] = AdaptiveModel(256,
                                                           track=self.track)
        return m

    def encode(self, enc, z):
        k = (z.bit_length() + 7) >> 3 or 1
        self.len_model.encode(enc, k - 1)
        for pos in range(k):
            self._byte_model(k, pos).encode(
                enc, (z >> ((k - 1 - pos) << 3)) & 0xFF)

    def decode(self, dec):
        k = self.len_model.decode(dec) + 1
        z = 0
        for pos in range(k):
            z = (z << 8) | self._byte_model(k, pos).decode(dec)
        return z

    def models(self):
        yield self.len_model
        yield from self.byte_models.values()


class SizeModels:
    """Sizes: a zero/nonzero flag (37% zeros in the sample), then value-1."""

    __slots__ = ("flag", "val")

    def __init__(self, track=False):
        self.flag = AdaptiveModel(2, track=track)
        self.val = IntModels(track=track)

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
    """Escape lines verbatim: byte length, then order-1 coded raw bytes."""

    __slots__ = ("length", "byte_ctx", "track")

    def __init__(self, track=False):
        self.length = IntModels(track=track)
        self.byte_ctx = {}
        self.track = track

    def _ctx(self, prev):
        m = self.byte_ctx.get(prev)
        if m is None:
            m = self.byte_ctx[prev] = AdaptiveModel(256, track=self.track)
        return m

    def encode(self, enc, blob):
        self.length.encode(enc, len(blob))
        prev = 0
        for b in blob:
            self._ctx(prev).encode(enc, b)
            prev = b

    def decode(self, dec):
        n = self.length.decode(dec)
        out = bytearray()
        prev = 0
        for _ in range(n):
            b = self._ctx(prev).decode(dec)
            out.append(b)
            prev = b
        return bytes(out)

    def models(self):
        yield from self.length.models()
        yield from self.byte_ctx.values()
