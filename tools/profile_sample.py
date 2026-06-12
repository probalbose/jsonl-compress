#!/usr/bin/env python3
"""Profile samples/sample_BTCUSDT.jsonl to drive compressor design.

Streams the file (never loads it whole). Reports:
  - zeroth-order byte entropy of the raw file
  - per-field structure, value distributions
  - timestamp / sequence-number delta statistics
  - price tick structure and intra-array delta structure
  - empirical entropy of candidate transformed streams
"""
import json
import math
import sys
from collections import Counter
from datetime import datetime

PATH = "samples/sample_BTCUSDT.jsonl"


def entropy_bits_per_symbol(counter):
    n = sum(counter.values())
    if n == 0:
        return 0.0, 0
    h = 0.0
    for c in counter.values():
        p = c / n
        h -= p * math.log2(p)
    return h, n


def fmt_dist(counter, top=8):
    n = sum(counter.values())
    items = counter.most_common(top)
    s = ", ".join(f"{k!r}:{c} ({100*c/n:.1f}%)" for k, c in items)
    extra = len(counter) - len(items)
    if extra > 0:
        s += f", … +{extra} more"
    return s


def iso_to_us(ts):
    # 2026-06-11T00:00:00.014000+00:00 -> microseconds since epoch
    dt = datetime.fromisoformat(ts)
    return int(dt.timestamp() * 1_000_000 + 0.5)


def main():
    byte_counts = Counter()
    raw_bytes = 0
    with open(PATH, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            raw_bytes += len(chunk)
            byte_counts.update(chunk)
    h0, _ = entropy_bits_per_symbol(byte_counts)
    print(f"== Raw file ==")
    print(f"size: {raw_bytes:,} bytes")
    print(f"zeroth-order byte entropy: {h0:.4f} bits/byte "
          f"-> H0 bound {raw_bytes*h0/8:,.0f} bytes ({h0/8*100:.1f}% of raw)")
    print(f"distinct byte values: {len(byte_counts)}")

    # Per-field accumulators
    n_rows = 0
    bad_lines = 0
    const_fields = {k: Counter() for k in
                    ("stream", "symbol", "payload.e", "payload.s")}
    et_str_matches_E = 0          # event_time string derivable from payload.E?
    et_suffix = Counter()         # event_time sub-ms residue
    e_deltas = Counter()          # payload.E ms deltas
    recv_deltas = Counter()       # received_at us deltas
    recv_minus_event = Counter()  # received_at - event_time (us), bucketed
    U_minus_prev_u = Counter()    # U - prev_u (Binance contiguity)
    u_minus_U = Counter()         # span of each update
    blen = Counter()
    alen = Counter()

    # price/size token structure
    price_decimals = Counter()    # digits after '.'
    price_tick_mod = Counter()    # price in 1e-8 units mod 1e6 (is it 0.01-tick?)
    size_decimals = Counter()
    size_zero = 0
    size_total = 0
    size_value_dist = Counter()   # size as 1e-8 int, capped dist
    price_tok_lens = Counter()
    # intra-array price deltas (in 0.01 ticks), bids and asks separately
    bid_tick_deltas = Counter()
    ask_tick_deltas = Counter()
    first_bid_vs_prev = Counter() # first bid price delta vs previous row's first bid
    digit_counter = Counter()     # digits of |delta| values, for entropy est

    prev_E = None
    prev_recv = None
    prev_u = None
    prev_first_bid = None

    with open(PATH, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
                if not (isinstance(o, dict) and set(o.keys()) ==
                        {"stream", "symbol", "event_time", "received_at", "payload"}):
                    bad_lines += 1
                    continue
            except Exception:
                bad_lines += 1
                continue
            n_rows += 1
            p = o["payload"]
            const_fields["stream"][o["stream"]] += 1
            const_fields["symbol"][o["symbol"]] += 1
            const_fields["payload.e"][p["e"]] += 1
            const_fields["payload.s"][p["s"]] += 1

            E = p["E"]
            # is event_time exactly E rendered as ISO with +00:00 and 6 dp?
            et = o["event_time"]
            us = E * 1000
            dt = datetime.utcfromtimestamp(us // 1_000_000)
            rendered = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{us % 1_000_000:06d}+00:00"
            if rendered == et:
                et_str_matches_E += 1
            else:
                et_suffix[et[-12:]] += 1

            if prev_E is not None:
                e_deltas[E - prev_E] += 1
            prev_E = E

            recv = iso_to_us(o["received_at"])
            if prev_recv is not None:
                recv_deltas[recv - prev_recv] += 1
            prev_recv = recv
            lag = recv - E * 1000
            recv_minus_event[min(lag // 100_000, 50)] += 1  # 0.1s buckets, cap

            U, u = p["U"], p["u"]
            if prev_u is not None:
                U_minus_prev_u[U - prev_u] += 1
            u_minus_U[u - U] += 1
            prev_u = u

            for side, ctr, dctr in (("b", blen, bid_tick_deltas),
                                    ("a", alen, ask_tick_deltas)):
                arr = p[side]
                ctr[len(arr)] += 1
                prev_ticks = None
                for px, sz in arr:
                    price_tok_lens[len(px)] += 1
                    if "." in px:
                        ip, fp = px.split(".")
                        price_decimals[len(fp)] += 1
                        ticks8 = int(ip) * 10**8 + int(fp.ljust(8, "0"))
                    else:
                        price_decimals[0] += 1
                        ticks8 = int(px) * 10**8
                    price_tick_mod[ticks8 % 1_000_000] += 1
                    t = ticks8 // 1_000_000  # 0.01-tick units
                    if prev_ticks is not None:
                        d = t - prev_ticks
                        dctr[max(min(d, 10**6), -10**6)] += 1
                        for ch in str(abs(d)):
                            digit_counter[ch] += 1
                    prev_ticks = t
                    if side == "b" and arr and px == arr[0][0]:
                        pass
                    ipz, fpz = sz.split(".")
                    size_decimals[len(fpz)] += 1
                    sval = int(ipz) * 10**8 + int(fpz.ljust(8, "0"))
                    size_total += 1
                    if sval == 0:
                        size_zero += 1
                    size_value_dist[min(sval, 10**10)] += 1
                if side == "b" and arr:
                    fb = arr[0][0]
                    ip, fp = fb.split(".")
                    t = int(ip) * 100 + int(fp[:2])
                    if prev_first_bid is not None:
                        first_bid_vs_prev[max(min(t - prev_first_bid, 10**6), -10**6)] += 1
                    prev_first_bid = t

    print(f"\n== Rows ==")
    print(f"conformant rows: {n_rows}, escape-channel lines: {bad_lines}")

    print(f"\n== Constant-candidate fields ==")
    for k, c in const_fields.items():
        print(f"{k}: {fmt_dist(c)}")

    print(f"\n== event_time vs payload.E ==")
    print(f"event_time string exactly derivable from E (ISO, 6dp, +00:00): "
          f"{et_str_matches_E}/{n_rows}")
    if et_suffix:
        print(f"  non-matching suffixes: {fmt_dist(et_suffix)}")

    def report_deltas(name, ctr):
        h, n = entropy_bits_per_symbol(ctr)
        vals = sorted(ctr)
        lo, hi = vals[0], vals[-1]
        mode = ctr.most_common(1)[0]
        neg = sum(c for v, c in ctr.items() if v < 0)
        print(f"{name}: n={n}, distinct={len(ctr)}, range=[{lo},{hi}], "
              f"mode={mode[0]} ({100*mode[1]/n:.1f}%), neg={neg}, "
              f"H={h:.3f} bits/sym")
        print(f"   top: {fmt_dist(ctr, 6)}")

    print(f"\n== Sequencing / timestamps ==")
    report_deltas("payload.E delta (ms)", e_deltas)
    report_deltas("received_at delta (us)", recv_deltas)
    report_deltas("recv-E lag (0.1s buckets, capped 50)", recv_minus_event)
    report_deltas("U - prev_u", U_minus_prev_u)
    report_deltas("u - U (events per update)", u_minus_U)

    print(f"\n== Array lengths ==")
    report_deltas("len(b)", blen)
    report_deltas("len(a)", alen)

    print(f"\n== Price token structure ==")
    print(f"decimals after '.': {fmt_dist(price_decimals)}")
    print(f"token lengths: {fmt_dist(price_tok_lens)}")
    tick_ok = price_tick_mod.get(0, 0)
    tick_n = sum(price_tick_mod.values())
    print(f"price multiple of 0.01: {tick_ok}/{tick_n} "
          f"({100*tick_ok/tick_n:.2f}%)")
    if len(price_tick_mod) > 1:
        print(f"  non-0.01 residues: {fmt_dist(price_tick_mod, 5)}")

    print(f"\n== Intra-array price deltas (0.01-tick units) ==")
    report_deltas("bid deltas (descending?)", bid_tick_deltas)
    report_deltas("ask deltas (ascending?)", ask_tick_deltas)
    report_deltas("first-bid delta row-to-row", first_bid_vs_prev)

    print(f"\n== Size token structure ==")
    print(f"decimals: {fmt_dist(size_decimals)}")
    print(f"zero sizes: {size_zero}/{size_total} ({100*size_zero/size_total:.1f}%)")
    h, n = entropy_bits_per_symbol(size_value_dist)
    print(f"size value (1e-8 int, capped) entropy: {h:.3f} bits/sym over "
          f"{len(size_value_dist)} distinct values, n={n}")
    print(f"top sizes: {fmt_dist(size_value_dist, 8)}")


if __name__ == "__main__":
    main()
