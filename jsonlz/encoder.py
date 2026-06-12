"""Streaming encoder: lines -> blocks of per-stream coded segments."""
from .arith import Encoder
from .codec import EscapeModels, IntModels, SizeModels
from .format import (append_uvarint, write_block, write_eof, write_header)
from .transforms import Schema, extract_candidate, infer_qexp, zigzag

DEFAULT_BLOCK_LINES = 4096
DEFAULT_BLOCK_BYTES = 1 << 24   # cap raw bytes per block: bounds memory


def _infer_schema(lines):
    """Schema from the first block's candidates, or None if it has none.

    Constants come from the first structurally valid row; quantum
    exponents are the largest powers of ten dividing every price/size
    among rows sharing those constants.
    """
    cands = [c for c in map(extract_candidate, lines) if c is not None]
    if not cands:
        return None
    constants = cands[0].constants
    matching = [c for c in cands if c.constants == constants]
    pexp = infer_qexp(p for c in matching
                      for side in (c.bids, c.asks) for p, _ in side)
    sexp = infer_qexp(s for c in matching
                      for side in (c.bids, c.asks) for _, s in side)
    return Schema(constants, pexp, sexp)


def _encode_int_segment(values, track):
    enc = Encoder()
    bank = IntModels(track=track)
    for z in values:
        bank.encode(enc, z)
    return enc.finish(), bank


def _encode_size_segment(values, track):
    enc = Encoder()
    bank = SizeModels(track=track)
    for v in values:
        bank.encode(enc, v)
    return enc.finish(), bank


def _encode_escape_segment(blobs, track):
    enc = Encoder()
    bank = EscapeModels(track=track)
    for blob in blobs:
        bank.encode(enc, blob)
    return enc.finish(), bank


def encode_block(lines, schema, stats=None):
    """One block of lines -> payload bytes. Self-contained: models and
    row-to-row references all start fresh (reviewer decision: localised
    corruption, parallel-decodable blocks)."""
    track = stats is not None
    cols = {name: [] for name in
            ("e_ms", "recv", "u_gap", "u_span", "len_b", "len_a",
             "bid_anchor", "bid_delta", "ask_anchor", "ask_delta",
             "size_b", "size_a")}
    esc_indices = []
    esc_blobs = []
    prev_e = prev_recv = prev_u = prev_bid = prev_ask = 0

    for i, line in enumerate(lines):
        row = None
        if schema is not None:
            cand = extract_candidate(line)
            if cand is not None:
                row = schema.quantize(cand)
                # the byte-exactness gate: values must reproduce the line
                if row is not None and schema.render_row(*row) != line:
                    row = None
        if row is None:
            esc_indices.append(i)
            esc_blobs.append(line)
            continue
        E, recv, U, u, bids, asks = row
        cols["e_ms"].append(zigzag(E - prev_e))
        cols["recv"].append(zigzag(recv - prev_recv))
        cols["u_gap"].append(zigzag(U - prev_u))
        cols["u_span"].append(zigzag(u - U))
        prev_e, prev_recv, prev_u = E, recv, u
        cols["len_b"].append(len(bids))
        cols["len_a"].append(len(asks))
        if bids:
            cols["bid_anchor"].append(zigzag(bids[0][0] - prev_bid))
            prev_bid = bids[0][0]
            for (hi, _), (lo, _) in zip(bids, bids[1:]):
                cols["bid_delta"].append(hi - lo - 1)  # strict descent: >= 0
            cols["size_b"].extend(s for _, s in bids)
        if asks:
            cols["ask_anchor"].append(zigzag(asks[0][0] - prev_ask))
            prev_ask = asks[0][0]
            for (lo, _), (hi, _) in zip(asks, asks[1:]):
                cols["ask_delta"].append(hi - lo - 1)
            cols["size_a"].extend(s for _, s in asks)

    segments = []
    for name in ("e_ms", "recv", "u_gap", "u_span", "len_b", "len_a",
                 "bid_anchor", "bid_delta", "ask_anchor", "ask_delta"):
        seg, bank = _encode_int_segment(cols[name], track)
        segments.append((name, seg, bank))
    for name in ("size_b", "size_a"):
        seg, bank = _encode_size_segment(cols[name], track)
        segments.append((name, seg, bank))
    seg, bank = _encode_escape_segment(esc_blobs, track)
    segments.append(("escape", seg, bank))

    if stats is not None:
        for name, seg, bank in segments:
            stats.add(name, bank.models(), len(seg))

    payload = bytearray()
    append_uvarint(payload, len(lines))
    append_uvarint(payload, len(esc_indices))
    prev = -1
    for idx in esc_indices:
        append_uvarint(payload, idx - prev - 1)
        prev = idx
    for _, seg, _ in segments:
        append_uvarint(payload, len(seg))
    for _, seg, _ in segments:
        payload += seg
    return bytes(payload)


def compress(in_path, out_path, block_lines=DEFAULT_BLOCK_LINES,
             block_bytes=DEFAULT_BLOCK_BYTES, stats=None):
    """Compress a JSONL file. Memory is bounded by the block caps (plus
    the largest single input line, which is the atomic unit)."""
    n_blocks = 0
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        schema = None
        header_written = False
        while True:
            lines, raw = [], 0
            while len(lines) < block_lines and raw < block_bytes:
                line = fin.readline()
                if not line:
                    break
                lines.append(line)
                raw += len(line)
            if not lines:
                break
            if not header_written:
                schema = _infer_schema(lines)
                write_header(fout, schema)
                header_written = True
            write_block(fout, encode_block(lines, schema, stats))
            n_blocks += 1
        if not header_written:        # empty input file
            write_header(fout, None)
        write_eof(fout)
    return schema, n_blocks
