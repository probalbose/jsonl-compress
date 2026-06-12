# Design notes — jsonl-compress

Running log of design decisions and trade-offs. Companion to `FORMAT.md`
(normative format spec) and `BRIEF.md` (requirements).

## Profiling results (drove everything below)

`tools/profile_sample.py` on `samples/sample_BTCUSDT.jsonl`
(78,299,366 bytes, 50,000 lines):

- Zeroth-order byte entropy 3.566 bits/byte → memoryless bound 34.9 MB.
  Only 48 distinct byte values. Baselines: gzip -9 = 9,873,596 B,
  zstd = 11,188,507 B, zstd -19 = 7,585,742 B.
- 49,996/50,000 lines are a single fixed shape and are **byte-identical to
  the compact `json.dumps` of their parse** — JSON structure carries zero
  information beyond the schema itself.
- 4 non-conformant lines: 2× two records glued on one line (missing
  newline), 2× blank lines. Handled by the escape channel.
- `stream`, `symbol`, `payload.e`, `payload.s` constant.
- `event_time` is exactly the ISO-8601 render of `payload.E` (ms, 6 d.p.,
  `+00:00`) in 100% of rows → derived field, costs one header constant.
- Prices: 100% have 8 decimals and are multiples of 0.01 → exact integer
  tick representation (t = 100·price). Sizes: 100% 8 decimals → integer
  1e-8 units; 37.2% exactly zero (book-level deletions).
- Bids strictly descending and asks strictly ascending within every row
  (2.05M entries, no exceptions) → intra-array deltas coded as
  magnitude−1, sign implied by side.
- **Brief's monotonicity assumption is violated row-to-row**: 17% of `E`
  deltas are negative (8,496), and negative `U − prev_u` gaps number
  exactly the same 8,496 → out-of-order arrival of whole events, not
  noise. All row-to-row deltas are therefore signed (zigzag).
- Order-0 entropy budget of the transformed streams ≈ 4.65 MB (5.9% of
  raw): bid deltas 1.19 MB, ask deltas 1.05 MB, sizes 2.05 MB, everything
  else ≈ 0.35 MB.

## Decisions

### Conformance = byte-equality under re-serialization
A line is conformant iff rebuilding it from its extracted column values
reproduces the original bytes exactly. The encoder *runs the decoder's
renderer* on each row and compares. This makes byte-exactness structural
rather than something to test for per token (numbers are never
float-parsed; price/size tokens map to integers only when their canonical
re-render matches). Cost: any cosmetic deviation (key order, whitespace)
sends the whole line to the escape channel. On this sample: 4 lines.
A differently-configured logger would need a schema-relaxation pass —
accepted for v1.

### Per-block model resets + CRC32 (reviewer decision)
Initially proposed persistent adaptive models across blocks (worth an
estimated 5–10%); reviewer chose per-block resets plus a per-block CRC32
over the compressed payload so corruption is detected and *localised*
instead of silently garbling the rest of the file. Blocks are therefore
fully independent (models and prev-row references reset; first row of
each block codes absolute values). **This buys parallel block decode for
free** — out of scope per the brief, but the format permits it: each
block is self-contained after the file header.

### `received_at` representation (measured)
Three candidates, costed under the real coding scheme (length symbol +
per-position byte models, order-0 conditional entropy):
successive µs deltas **100.8 KB**; latency `received_at − E` 111.1 KB;
latency deltas 108.7 KB. Network latency jitter is a noisier process
than inter-arrival time. Kept: successive deltas. The bottom microsecond
digit is uniform (3.32 bits) — genuinely incompressible noise.

### Per-file quantum exponents (reviewer directive, promoted to v1)
Prices and sizes are coded as integer multiples of per-file quanta
10^pexp / 10^sexp (in 1e-8 units), inferred from the first block and
stored in the header — coding prices in raw 1e-8 units would inflate
every delta by the quantum (≈ +1 byte per delta ≈ +2 MB on the BTC
sample). Inference over the first block (~650k price tokens) makes a
spuriously coarse quantum vanishingly unlikely; a later off-quantum
row escapes rather than corrupting anything. Validated on
SAMPLE_XRPUSDT.jsonl: tick inferred 1e-4, size step 1e-1, byte-exact
round trip.

### Arithmetic coder, no Huffman fallback
Witten–Neal–Cleary integer arithmetic coder with renormalisation and
underflow handling; adaptive frequency models on a Fenwick tree, counts
halved when the total hits 2¹⁶ (bounds coder arithmetic; gives a recency
horizon). Every stream's alphabet is ≤ 256 (integers are decomposed into
a byte-length symbol + bytes), so no stream justifies a Huffman fallback:
the skewed, small-alphabet distributions here are exactly where
arithmetic coding's fractional-bit advantage matters (e.g. the bid-delta
length symbol is nearly deterministic — small fraction of a bit each).

### Entropy accounting definition
Per stream: empirical entropy = Σ over model contexts of the order-0
entropy of the symbol sequence actually fed to that context (i.e. the
conditional entropy given the model's context), measured per block to
match the reset discipline. Coded size must land within 5% of this.
This is the honest comparison: it scores the coder against the model it
actually used, not against a weaker unconditioned model.

### The 5% entropy-gap investigation (measured)
First full benchmark (4096-line blocks): the four high-volume streams
(96% of output) coded within 1.2% of their per-context conditional
entropy, but seven sparse streams breached 5% (anchors +10.5%, u_gap
+10.3%, recv +6.9%, escape +13.4%). Hypotheses tested, in order:

1. **Adaptation increment** (24 → 48 → 96 → 160): monotonically
   *worse* (total gap +3.3% → +6.1% on XRPUSDT). Estimation noise and
   window-shortening dominate the prior-mass effect. Rejected.
2. **Alphabet size** (byte → nibble decomposition, low nibble
   conditioned on high; identical conditional entropy by chain rule):
   coded size moved ~0.06%. The warm-up cost is not the 256-symbol
   uniform prior. Largely rejected — kept for the sparse streams and
   escape (small but free win, ~0.2 KB), byte layout kept for the
   high-volume streams (fewer coder ops).
3. **Block size** (4096 → 8192 → 16384 lines): gaps fall roughly as
   1/n (u_gap +9.5% → +6.4% → ≤5%; XRPUSDT fully passes at 16384).
   This identified the true cause: **parametric redundancy** — the
   first-occurrence tax a one-pass coder pays on each distinct symbol,
   which the per-block empirical entropy (a two-pass oracle that knows
   the counts in advance) never pays. ~K·log(n)/2n per symbol, hence
   the 1/n scaling.

Proof that the floor is real: per-stream Krichevsky–Trofimov bounds
(minimax-optimal one-pass cost for the same contexts on the same
data). At 16384-line blocks every stream codes within ±2.5% of KT;
bid_delta sits exactly on it and escape *beats* KT by 5% (increment-24
adapts faster than KT's +1/2 on repetitive JSON-ish text). The
remaining >5% conditional-entropy flags (BTCUSDT: anchors ~+5.7%,
escape +9.0%) are therefore the sequential-coding floor, not coder
loss; the benchmark prints the KT column alongside the gap so the
criterion is answered with a bound, not an excuse. Closing the
residual would require either bigger blocks (less corruption
localisation — reviewer chose 16384 as the balance point) or
transmitting frequency tables (costs more than it saves at this
block size).

### Header integrity (reviewer amendment)
The header originally had no CRC; a corrupted pexp/sexp would decode
the whole file to plausible-but-wrong values with every block CRC
passing. Header now carries its own CRC32 (magic through JSON),
verified before any field is used.

## Where it would parallelise (not implemented)
Blocks are independent after the header: encode could fan out blocks to
workers and concatenate; decode could scan block headers (length-
prefixed) and decode blocks concurrently. Kept single-threaded per the
brief.
