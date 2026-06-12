"""Streaming decoder: blocks -> original lines, byte-exact by construction."""
from .arith import Decoder
from .codec import (EscapeModels, IntModels, NIBBLE_INT_STREAMS,
                    NIBBLE_SIZE_STREAMS, SizeModels)
from .format import (ByteReader, FormatError, SEGMENT_NAMES, read_block,
                     read_header)
from .transforms import Schema, unzigzag


def _decode_ints(seg, count, name):
    dec = Decoder(seg)
    bank = IntModels(nibble=name in NIBBLE_INT_STREAMS)
    return [bank.decode(dec) for _ in range(count)]


def _decode_sizes(seg, count, name):
    dec = Decoder(seg)
    bank = SizeModels(nibble=name in NIBBLE_SIZE_STREAMS)
    return [bank.decode(dec) for _ in range(count)]


def _decode_escapes(seg, count):
    dec = Decoder(seg)
    bank = EscapeModels()
    return [bank.decode(dec) for _ in range(count)]


def decode_block(payload, schema, fout, block_index):
    r = ByteReader(payload)
    n_lines = r.uvarint()
    n_esc = r.uvarint()
    if n_esc > n_lines:
        raise FormatError(f"block {block_index}: {n_esc} escapes > "
                          f"{n_lines} lines")
    esc_indices = []
    prev = -1
    for _ in range(n_esc):
        prev += r.uvarint() + 1
        esc_indices.append(prev)
    if esc_indices and esc_indices[-1] >= n_lines:
        raise FormatError(f"block {block_index}: escape index out of range")
    seg_lens = [r.uvarint() for _ in range(len(SEGMENT_NAMES))]
    segs = dict(zip(SEGMENT_NAMES, (r.take(n) for n in seg_lens)))
    if r.pos != len(payload):
        raise FormatError(f"block {block_index}: trailing bytes in payload")

    n_rows = n_lines - n_esc
    if n_rows and schema is None:
        raise FormatError(f"block {block_index}: rows present but header "
                          f"has no schema")

    len_b = _decode_ints(segs["len_b"], n_rows, "len_b")
    len_a = _decode_ints(segs["len_a"], n_rows, "len_a")
    e_ms = _decode_ints(segs["e_ms"], n_rows, "e_ms")
    recv = _decode_ints(segs["recv"], n_rows, "recv")
    u_gap = _decode_ints(segs["u_gap"], n_rows, "u_gap")
    u_span = _decode_ints(segs["u_span"], n_rows, "u_span")
    bid_anchor = iter(_decode_ints(segs["bid_anchor"],
                                   sum(1 for n in len_b if n), "bid_anchor"))
    bid_delta = iter(_decode_ints(segs["bid_delta"],
                                  sum(n - 1 for n in len_b if n),
                                  "bid_delta"))
    ask_anchor = iter(_decode_ints(segs["ask_anchor"],
                                   sum(1 for n in len_a if n), "ask_anchor"))
    ask_delta = iter(_decode_ints(segs["ask_delta"],
                                  sum(n - 1 for n in len_a if n),
                                  "ask_delta"))
    size_b = iter(_decode_sizes(segs["size_b"], sum(len_b), "size_b"))
    size_a = iter(_decode_sizes(segs["size_a"], sum(len_a), "size_a"))
    escapes = iter(_decode_escapes(segs["escape"], n_esc))

    esc_set = set(esc_indices)
    prev_e = prev_recv = prev_u = prev_bid = prev_ask = 0
    row_i = 0
    for i in range(n_lines):
        if i in esc_set:
            fout.write(next(escapes))
            continue
        E = prev_e + unzigzag(e_ms[row_i])
        rc = prev_recv + unzigzag(recv[row_i])
        U = prev_u + unzigzag(u_gap[row_i])
        u = U + unzigzag(u_span[row_i])
        prev_e, prev_recv, prev_u = E, rc, u

        bids = []
        if len_b[row_i]:
            t = prev_bid + unzigzag(next(bid_anchor))
            prev_bid = t
            bids.append((t, next(size_b)))
            for _ in range(len_b[row_i] - 1):
                t -= next(bid_delta) + 1
                bids.append((t, next(size_b)))
        asks = []
        if len_a[row_i]:
            t = prev_ask + unzigzag(next(ask_anchor))
            prev_ask = t
            asks.append((t, next(size_a)))
            for _ in range(len_a[row_i] - 1):
                t += next(ask_delta) + 1
                asks.append((t, next(size_a)))

        fout.write(schema.render_row(E, rc, U, u, bids, asks))
        row_i += 1


def decompress(in_path, out_path):
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        meta = read_header(fin)
        s = meta["schema"]
        schema = None
        if s is not None:
            consts = s.get("constants") if isinstance(s, dict) else None
            pexp, sexp = (s.get("pexp"), s.get("sexp")) if isinstance(
                s, dict) else (None, None)
            if (not isinstance(consts, list) or len(consts) != 4
                    or not all(isinstance(x, str) for x in consts)
                    or not (type(pexp) is int and 0 <= pexp <= 8)
                    or not (type(sexp) is int and 0 <= sexp <= 8)):
                raise FormatError("invalid schema in header")
            schema = Schema(tuple(consts), pexp, sexp)
        block_index = 0
        while True:
            payload = read_block(fin, block_index)
            if payload is None:
                return block_index
            decode_block(payload, schema, fout, block_index)
            block_index += 1
