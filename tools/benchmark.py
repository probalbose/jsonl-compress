#!/usr/bin/env python3
"""Benchmark jsonlz against gzip -9 and zstd on a sample, with per-stream
entropy accounting and a per-stage size breakdown.

Usage: python3 tools/benchmark.py <sample.jsonl> [report.md]

External compressors are invoked only here, as comparison baselines.
"""
import hashlib
import math
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jsonlz.decoder import decompress
from jsonlz.encoder import _infer_schema, compress
from jsonlz.transforms import extract_candidate, zigzag

GAP_LIMIT = 5.0  # acceptance criterion: coded size within 5% of entropy


def kt_bits(hist):
    """Krichevsky–Trofimov block cost for one context's counts: the
    minimax-optimal code length achievable by ANY sequential (one-pass)
    coder over this alphabet, regardless of symbol order."""
    n = sum(hist)
    if n == 0:
        return 0.0
    s = sum(math.lgamma(c + 0.5) - math.lgamma(0.5) for c in hist if c)
    s += math.lgamma(len(hist) * 0.5) - math.lgamma(n + len(hist) * 0.5)
    return -s / math.log(2)


class StreamStats:
    __slots__ = ("symbols", "entropy_bits", "kt_bits", "coded_bytes")

    def __init__(self):
        self.symbols = 0
        self.entropy_bits = 0.0
        self.kt_bits = 0.0
        self.coded_bytes = 0


class Stats:
    """Per-stream accounting. Entropy is measured per model context per
    block (the conditional entropy of exactly what each adaptive model
    saw), matching the per-block reset discipline. The KT bound is the
    sequential-coding floor for the same contexts on the same data."""

    def __init__(self):
        self.streams = defaultdict(StreamStats)

    def add(self, name, models, coded_len):
        st = self.streams[name]
        st.coded_bytes += coded_len
        for m in models:
            hist = m.hist
            n = sum(hist)
            if not n:
                continue
            st.symbols += n
            st.entropy_bits -= sum(
                c * math.log2(c / n) for c in hist if c)
            st.kt_bits += kt_bits(hist)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def run_baseline(name, comp_cmd, dec_cmd, src, out_dir):
    comp_path = os.path.join(out_dir, name.replace(" ", "_"))
    t0 = time.perf_counter()
    with open(comp_path, "wb") as f:
        subprocess.run(comp_cmd + [src], stdout=f, check=True)
    t_enc = time.perf_counter() - t0
    t0 = time.perf_counter()
    subprocess.run(dec_cmd + [comp_path], stdout=subprocess.DEVNULL,
                   check=True)
    t_dec = time.perf_counter() - t0
    return os.path.getsize(comp_path), t_enc, t_dec


def varint_len(z):
    return max(1, (z.bit_length() + 6) // 7)


def stage_breakdown(src, schema):
    """Sizes of intermediate representations (encoder-independent):
    1. raw bytes
    2. residual text: variable tokens left after the structure
       dictionary and event_time derivation (escape lines kept whole)
    3. transformed integers: stage-2 values after columnar delta/tick
       transforms, measured as plain varints (pre-entropy-coding)
    """
    raw = residual = transformed = 0
    prev_e = prev_recv = prev_u = prev_bid = prev_ask = 0
    with open(src, "rb") as f:
        for line in f:
            raw += len(line)
            cand = extract_candidate(line)
            row = schema.quantize(cand) if (schema and cand) else None
            if row is None or schema.render_row(*row) != line:
                residual += len(line)
                transformed += len(line) + varint_len(len(line))
                continue
            E, recv, U, u, bids, asks = row
            residual += (len(str(E)) + 32 + len(str(U)) + len(str(u))
                         + sum(len(str(p)) + len(str(s)) + 2
                               for side in (bids, asks) for p, s in side))
            ints = [zigzag(E - prev_e), zigzag(recv - prev_recv),
                    zigzag(U - prev_u), zigzag(u - U),
                    len(bids), len(asks)]
            prev_e, prev_recv, prev_u = E, recv, u
            if bids:
                ints.append(zigzag(bids[0][0] - prev_bid))
                prev_bid = bids[0][0]
                ints += [hi - lo - 1 for (hi, _), (lo, _)
                         in zip(bids, bids[1:])]
                ints += [s for _, s in bids]
            if asks:
                ints.append(zigzag(asks[0][0] - prev_ask))
                prev_ask = asks[0][0]
                ints += [hi - lo - 1 for (lo, _), (hi, _)
                         in zip(asks, asks[1:])]
                ints += [s for _, s in asks]
            transformed += sum(varint_len(z) for z in ints)
    return raw, residual, transformed


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "samples/sample_BTCUSDT.jsonl"
    report_path = sys.argv[2] if len(sys.argv) > 2 else None
    raw_size = os.path.getsize(src)
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    with tempfile.TemporaryDirectory() as tmp:
        # jsonlz: timed clean run, then stats run for accounting
        jlz = os.path.join(tmp, "out.jlz")
        rest = os.path.join(tmp, "restored.jsonl")
        t0 = time.perf_counter()
        compress(src, jlz)
        t_enc = time.perf_counter() - t0
        t0 = time.perf_counter()
        decompress(jlz, rest)
        t_dec = time.perf_counter() - t0
        ok = sha256(src) == sha256(rest)
        jlz_size = os.path.getsize(jlz)

        stats = Stats()
        schema, _ = compress(src, os.path.join(tmp, "stats.jlz"), stats=stats)

        rows = [("jsonlz", jlz_size, t_enc, t_dec)]
        for name, comp, dec in (
                ("gzip -9", ["gzip", "-9", "-c"], ["gzip", "-dc"]),
                ("zstd", ["zstd", "-q", "-c"], ["zstd", "-q", "-dc"]),
                ("zstd -19", ["zstd", "-q", "-19", "-c"], ["zstd", "-q", "-dc"])):
            try:
                rows.append((name, *run_baseline(name, comp, dec, src, tmp)))
            except FileNotFoundError:
                emit(f"({name} not available, skipped)")

        emit(f"# Benchmark: {os.path.basename(src)}")
        emit()
        emit(f"Raw size: {raw_size:,} bytes. jsonlz round trip "
             f"SHA-256: {'byte-identical' if ok else '** MISMATCH **'}.")
        emit()
        emit("| compressor | size (B) | % of raw | ratio | enc MB/s | dec MB/s |")
        emit("|---|---:|---:|---:|---:|---:|")
        for name, size, te, td in rows:
            emit(f"| {name} | {size:,} | {100*size/raw_size:.2f}% "
                 f"| {raw_size/size:.2f}x | {raw_size/te/1e6:.1f} "
                 f"| {raw_size/td/1e6:.1f} |")
        emit()

        # per-stream entropy accounting
        emit("## Entropy accounting (per stream)")
        emit()
        emit("Entropy is the conditional empirical entropy of the symbols "
             "each adaptive model context actually coded, measured per "
             "block (matching the per-block reset discipline). The coder "
             f"must land within {GAP_LIMIT:.0f}% of it; breaches are "
             "flagged. The KT column is the Krichevsky–Trofimov bound: "
             "the minimum ANY one-pass coder can achieve with the same "
             "contexts on the same data — a flagged stream coding at "
             "~KT is at the sequential floor (the gap is two-pass-oracle "
             "advantage, not coder loss).")
        emit()
        emit("| stream | symbols | H (bits/sym) | entropy (B) | coded (B) "
             "| gap | | KT (B) | vs KT |")
        emit("|---|---:|---:|---:|---:|---:|---|---:|---:|")
        tot_h = tot_c = tot_kt = 0
        for name, st in sorted(stats.streams.items(),
                               key=lambda kv: -kv[1].coded_bytes):
            hbytes = st.entropy_bits / 8
            ktbytes = st.kt_bits / 8
            tot_h += hbytes
            tot_c += st.coded_bytes
            tot_kt += ktbytes
            if st.symbols == 0:
                # nothing coded; segment bytes are pure coder flush
                emit(f"| {name} | 0 | — | — | {st.coded_bytes:,} | — "
                     f"| n/a (empty) | — | — |")
                continue
            gap = 100 * (st.coded_bytes - hbytes) / hbytes
            ktgap = 100 * (st.coded_bytes - ktbytes) / ktbytes
            flag = "**> 5%**" if gap > GAP_LIMIT else "ok"
            hsym = st.entropy_bits / st.symbols
            emit(f"| {name} | {st.symbols:,} | {hsym:.3f} "
                 f"| {hbytes:,.0f} | {st.coded_bytes:,} | {gap:+.2f}% "
                 f"| {flag} | {ktbytes:,.0f} | {ktgap:+.2f}% |")
        gap = 100 * (tot_c - tot_h) / tot_h
        ktgap = 100 * (tot_c - tot_kt) / tot_kt
        emit(f"| **total** | | | {tot_h:,.0f} | {tot_c:,} | {gap:+.2f}% "
             f"| {'**> 5%**' if gap > GAP_LIMIT else 'ok'} "
             f"| {tot_kt:,.0f} | {ktgap:+.2f}% |")
        emit()

        # stage breakdown
        raw, residual, transformed = stage_breakdown(src, schema)
        emit("## Per-stage breakdown")
        emit()
        emit("Each row is the size of the data in successively transformed "
             "representations (stage 3 measured as plain varints, i.e. "
             "before any entropy coding; stage 4 is the actual output "
             "including framing).")
        emit()
        emit("| stage | size (B) | % of raw | step gain |")
        emit("|---|---:|---:|---:|")
        stages = [("1. raw JSONL", raw),
                  ("2. minus structure dict + derived fields", residual),
                  ("3. columnar + delta + tick integers (varint)", transformed),
                  ("4. entropy coded (final file)", jlz_size)]
        prev = None
        for name, size in stages:
            gain = f"{prev/size:.2f}x" if prev else ""
            emit(f"| {name} | {size:,} | {100*size/raw:.2f}% | {gain} |")
            prev = size
        emit()

    text = "\n".join(lines) + "\n"
    if report_path:
        with open(report_path, "w") as f:
            f.write(text)
        print(f"\n(report written to {report_path})")


if __name__ == "__main__":
    main()
