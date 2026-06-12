"""Adaptive integer arithmetic coder with renormalisation.

Witten–Neal–Cleary construction (CACM 1987) on 32-bit registers:
the coder narrows an integer interval [low, high] in proportion to
symbol probabilities and emits bits as the interval's leading bits
settle, with the classic underflow ("pending bits") treatment when
the interval straddles the midpoint.

Probabilities come from `AdaptiveModel`: per-context order-0 frequency
tables on a Fenwick tree, incremented after every symbol and halved when
the total reaches MAX_TOTAL. Encoder and decoder update identically, so
no tables are transmitted.

Invariants that make the integer arithmetic exact:
  - after renormalisation, high - low + 1 > QUARTER = 2^30
  - model totals never exceed MAX_TOTAL = 2^16 <= QUARTER, so
    range * cum_freq // total never maps distinct symbols to an
    empty subinterval.
"""

PRECISION = 32
FULL = 1 << PRECISION
HALF = FULL >> 1
QUARTER = FULL >> 2
THREE_QUARTER = HALF + QUARTER
MASK = FULL - 1

MAX_TOTAL = 1 << 16


class Encoder:
    """Arithmetic encoder writing to an internal buffer; get bytes via finish()."""

    def __init__(self):
        self.low = 0
        self.high = MASK
        self.pending = 0
        self._out = bytearray()
        self._bitbuf = 0
        self._nbits = 0

    def _emit(self, bit):
        self._bitbuf = (self._bitbuf << 1) | bit
        self._nbits += 1
        if self._nbits == 8:
            self._out.append(self._bitbuf)
            self._bitbuf = 0
            self._nbits = 0

    def _emit_with_pending(self, bit):
        self._emit(bit)
        inv = bit ^ 1
        while self.pending:
            self._emit(inv)
            self.pending -= 1

    def encode(self, cum_lo, cum_hi, total):
        """Encode a symbol occupying [cum_lo, cum_hi) of [0, total)."""
        rng = self.high - self.low + 1
        self.high = self.low + (rng * cum_hi) // total - 1
        self.low = self.low + (rng * cum_lo) // total
        while True:
            if self.high < HALF:
                self._emit_with_pending(0)
            elif self.low >= HALF:
                self._emit_with_pending(1)
                self.low -= HALF
                self.high -= HALF
            elif self.low >= QUARTER and self.high < THREE_QUARTER:
                self.pending += 1
                self.low -= QUARTER
                self.high -= QUARTER
            else:
                break
            self.low <<= 1
            self.high = (self.high << 1) | 1

    def finish(self):
        """Flush the final interval and return the coded bytes."""
        # One more bit (plus pending) pins the interval; the decoder's
        # zero-padding past end-of-stream supplies the rest.
        self.pending += 1
        self._emit_with_pending(0 if self.low < QUARTER else 1)
        while self._nbits:
            self._emit(0)
        return bytes(self._out)


class Decoder:
    """Arithmetic decoder over a bytes-like segment. Reads zeros past the end."""

    def __init__(self, data):
        self._data = data
        self._pos = 0
        self._bitbuf = 0
        self._nbits = 0
        self.low = 0
        self.high = MASK
        self.code = 0
        for _ in range(PRECISION):
            self.code = (self.code << 1) | self._read_bit()

    def _read_bit(self):
        if self._nbits == 0:
            if self._pos < len(self._data):
                self._bitbuf = self._data[self._pos]
                self._pos += 1
            else:
                self._bitbuf = 0
            self._nbits = 8
        self._nbits -= 1
        return (self._bitbuf >> self._nbits) & 1

    def target(self, total):
        """Cumulative-frequency value of the next symbol, in [0, total)."""
        rng = self.high - self.low + 1
        return ((self.code - self.low + 1) * total - 1) // rng

    def consume(self, cum_lo, cum_hi, total):
        """Narrow the interval for the symbol identified via target()."""
        rng = self.high - self.low + 1
        self.high = self.low + (rng * cum_hi) // total - 1
        self.low = self.low + (rng * cum_lo) // total
        while True:
            if self.high < HALF:
                pass
            elif self.low >= HALF:
                self.low -= HALF
                self.high -= HALF
                self.code -= HALF
            elif self.low >= QUARTER and self.high < THREE_QUARTER:
                self.low -= QUARTER
                self.high -= QUARTER
                self.code -= QUARTER
            else:
                break
            self.low <<= 1
            self.high = (self.high << 1) | 1
            self.code = (self.code << 1) | self._read_bit()


class AdaptiveModel:
    """Order-0 adaptive frequency model over a fixed alphabet of size n.

    Frequencies live in a Fenwick (binary indexed) tree: cumulative
    lookups, point updates, and decode-side symbol search are all
    O(log n). Every symbol starts at count 1 (always codable); counts
    grow by `increment` per occurrence and are halved (rounding up)
    when the total exceeds MAX_TOTAL, giving a recency horizon.

    With track=True the model records a histogram of symbols it coded,
    used by the benchmark's entropy accounting.
    """

    __slots__ = ("n", "increment", "total", "_tree", "_top", "hist")

    def __init__(self, n, increment=24, track=False):
        self.n = n
        self.increment = increment
        self._tree = tree = [0] * (n + 1)
        for i in range(1, n + 1):
            tree[i] += 1
            j = i + (i & -i)
            if j <= n:
                tree[j] += tree[i]
        self.total = n
        top = 1
        while (top << 1) <= n:
            top <<= 1
        self._top = top
        self.hist = [0] * n if track else None

    def _cum(self, sym):
        """Sum of frequencies of symbols 0..sym-1."""
        i, s, tree = sym, 0, self._tree
        while i:
            s += tree[i]
            i -= i & -i
        return s

    def _find(self, f):
        """Symbol whose cumulative interval contains f; returns (sym, cum_lo)."""
        idx, rem, bit, tree, n = 0, f, self._top, self._tree, self.n
        while bit:
            nxt = idx + bit
            if nxt <= n and tree[nxt] <= rem:
                idx = nxt
                rem -= tree[nxt]
            bit >>= 1
        return idx, f - rem

    def _bump(self, sym):
        if self.hist is not None:
            self.hist[sym] += 1
        i, n, tree, inc = sym + 1, self.n, self._tree, self.increment
        while i <= n:
            tree[i] += inc
            i += i & -i
        self.total += inc
        if self.total > MAX_TOTAL:
            self._rescale()

    def _rescale(self):
        n = self.n
        freqs = [0] * n
        prev = 0
        for s in range(n):
            c = self._cum(s + 1)
            freqs[s] = c - prev
            prev = c
        self._tree = tree = [0] * (n + 1)
        total = 0
        for i in range(1, n + 1):
            fq = (freqs[i - 1] + 1) >> 1
            total += fq
            tree[i] += fq
            j = i + (i & -i)
            if j <= n:
                tree[j] += tree[i]
        self.total = total

    def encode(self, enc, sym):
        lo = self._cum(sym)
        hi = self._cum(sym + 1)
        enc.encode(lo, hi, self.total)
        self._bump(sym)

    def decode(self, dec):
        sym, lo = self._find(dec.target(self.total))
        hi = self._cum(sym + 1)
        dec.consume(lo, hi, self.total)
        self._bump(sym)
        return sym
