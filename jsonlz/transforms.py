"""Invertible transforms between JSONL rows and integer column values.

The contract that guarantees byte-exactness: a line is *conformant* only
if `render_row` over its extracted values reproduces the original bytes.
The encoder performs that comparison for every row; anything that fails
travels the escape channel verbatim. Decoding therefore never guesses —
it re-runs the same renderer over the same values.

Numeric tokens are never parsed as floats. Prices/sizes map to integers
in 1e-8 units via exact string decomposition, then to multiples of a
per-file quantum 10^k (k inferred by the encoder, stored in the header).
"""
import json
import re
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)

# Conformant rows fit this exact shape (v1 schema).
TOP_KEYS = ("stream", "symbol", "event_time", "received_at", "payload")
PAYLOAD_KEYS = ("e", "E", "s", "U", "u", "b", "a")

# Numeric token: no leading zeros, exactly 8 decimals (exchange render).
_NUM8_RE = re.compile(r"\A(0|[1-9][0-9]*)\.([0-9]{8})\Z")

# Bound on any integer the codec will code (zigzag fits in 10 bytes).
VALUE_BOUND = 1 << 62


def zigzag(v):
    """Map signed int to unsigned: 0,-1,1,-2,2 ... -> 0,1,2,3,4 ..."""
    return (v << 1) if v >= 0 else ((-v) << 1) - 1


def unzigzag(z):
    return (z >> 1) if (z & 1) == 0 else -((z + 1) >> 1)


def ts_to_us(s):
    """ISO-8601 string -> integer microseconds since epoch (exact)."""
    dt = datetime.fromisoformat(s)
    return (dt - _EPOCH) // _US


def us_to_iso(us):
    """Integer microseconds -> canonical 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'."""
    dt = _EPOCH + timedelta(microseconds=us)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
            f".{dt.microsecond:06d}+00:00")


def token_to_v8(tok):
    """'61510.99000000' -> 6151099000000 (1e-8 units), or None if not canonical."""
    m = _NUM8_RE.match(tok)
    if m is None:
        return None
    return int(m.group(1)) * 10**8 + int(m.group(2))


def v8_to_token(v8):
    return f"{v8 // 10**8}.{v8 % 10**8:08d}"


def infer_qexp(values):
    """Largest k in [0,8] such that every value is a multiple of 10^k."""
    k = 8
    for v in values:
        while k and v % (10 ** k):
            k -= 1
        if k == 0:
            break
    return k


class Candidate:
    """Structurally valid row, prices/sizes still in raw 1e-8 units."""
    __slots__ = ("constants", "E", "recv_us", "U", "u", "bids", "asks")

    def __init__(self, constants, E, recv_us, U, u, bids, asks):
        self.constants = constants  # (stream, symbol, e, s)
        self.E = E
        self.recv_us = recv_us
        self.U = U
        self.u = u
        self.bids = bids            # [(price_v8, size_v8)], strictly descending prices
        self.asks = asks            # strictly ascending prices


def _levels(raw):
    """Validate [[price, size], ...] and convert tokens to 1e-8 ints."""
    if not isinstance(raw, list):
        return None
    out = []
    for entry in raw:
        if not (isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[0], str) and isinstance(entry[1], str)):
            return None
        p = token_to_v8(entry[0])
        s = token_to_v8(entry[1])
        if p is None or s is None or p >= VALUE_BOUND or s >= VALUE_BOUND:
            return None
        out.append((p, s))
    return out


def _strictly_monotonic(levels, descending):
    prev = None
    for p, _ in levels:
        if prev is not None and (p >= prev if descending else p <= prev):
            return False
        prev = p
    return True


def _is_int(v):
    return type(v) is int  # excludes bool


def extract_candidate(line):
    """bytes line (incl. trailing newline) -> Candidate, or None to escape."""
    if not line.endswith(b"\n"):
        return None
    try:
        o = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(o, dict) or tuple(o.keys()) != TOP_KEYS:
        return None
    p = o["payload"]
    if not isinstance(p, dict) or tuple(p.keys()) != PAYLOAD_KEYS:
        return None
    if not all(isinstance(o[k], str) for k in ("stream", "symbol",
                                               "event_time", "received_at")):
        return None
    if not (isinstance(p["e"], str) and isinstance(p["s"], str)):
        return None
    E, U, u = p["E"], p["U"], p["u"]
    if not (_is_int(E) and _is_int(U) and _is_int(u)):
        return None
    if not (0 <= E < VALUE_BOUND // 1000 and 0 <= U < VALUE_BOUND
            and 0 <= u < VALUE_BOUND):
        return None
    try:
        recv_us = ts_to_us(o["received_at"])
    except ValueError:
        return None
    if not 0 <= recv_us < VALUE_BOUND:
        return None
    # event_time must be the canonical render of E; it is then derived, not stored
    try:
        if o["event_time"] != us_to_iso(E * 1000):
            return None
    except OverflowError:
        return None
    bids = _levels(p["b"])
    asks = _levels(p["a"])
    if bids is None or asks is None:
        return None
    if not (_strictly_monotonic(bids, descending=True)
            and _strictly_monotonic(asks, descending=False)):
        return None
    return Candidate((o["stream"], o["symbol"], p["e"], p["s"]),
                     E, recv_us, U, u, bids, asks)


class Schema:
    """Per-file parameters: constant fields and price/size quantum exponents.

    Owns the canonical renderer. Fragments between variable fields are
    precomputed once (constants are embedded via json.dumps, so any
    escaping matches what json would emit).
    """
    __slots__ = ("constants", "pexp", "sexp", "_pq", "_sq",
                 "_f0", "_f1", "_f2", "_f3", "_f4", "_f5", "_f6")

    def __init__(self, constants, pexp, sexp):
        stream, symbol, e, s = constants
        self.constants = (stream, symbol, e, s)
        self.pexp = pexp
        self.sexp = sexp
        self._pq = 10 ** pexp
        self._sq = 10 ** sexp
        self._f0 = ('{"stream":' + json.dumps(stream)
                    + ',"symbol":' + json.dumps(symbol)
                    + ',"event_time":"')
        self._f1 = '","received_at":"'
        self._f2 = '","payload":{"e":' + json.dumps(e) + ',"E":'
        self._f3 = ',"s":' + json.dumps(s) + ',"U":'
        self._f4 = ',"u":'
        self._f5 = ',"b":'
        self._f6 = ',"a":'

    def quantize(self, cand):
        """Candidate -> (E, recv_us, U, u, bids, asks) in quantum units,
        or None if constants differ or any value is off-quantum."""
        if cand.constants != self.constants:
            return None
        pq, sq = self._pq, self._sq
        sides = []
        for levels in (cand.bids, cand.asks):
            q = []
            for p, s in levels:
                if p % pq or s % sq:
                    return None
                q.append((p // pq, s // sq))
            sides.append(q)
        return (cand.E, cand.recv_us, cand.U, cand.u, sides[0], sides[1])

    def render_row(self, E, recv_us, U, u, bids, asks):
        """Values in quantum units -> exact line bytes (with newline)."""
        pq, sq = self._pq, self._sq

        def side(levels):
            if not levels:
                return "[]"
            return ("[[\""
                    + "\"],[\"".join(
                        v8_to_token(p * pq) + "\",\"" + v8_to_token(s * sq)
                        for p, s in levels)
                    + "\"]]")

        return (self._f0 + us_to_iso(E * 1000)
                + self._f1 + us_to_iso(recv_us)
                + self._f2 + str(E)
                + self._f3 + str(U)
                + self._f4 + str(u)
                + self._f5 + side(bids)
                + self._f6 + side(asks)
                + "}}\n").encode("utf-8")
