# jsonlz container format, version 1

Normative specification. An independent decoder implementing exactly
what is written here reproduces the original file byte for byte.

All multi-byte plain integers are little-endian. "uvarint" is LEB128:
7 value bits per byte, low bits first, high bit set on all but the
final byte. "zigzag" maps signed to unsigned integers:
`z = 2v` for `v >= 0`, `z = -2v - 1` for `v < 0`.

CRC32 is the IEEE 802.3 CRC (reflected polynomial `0xEDB88320`,
initial value `0xFFFFFFFF`, final XOR `0xFFFFFFFF`) — identical to
zlib's `crc32`.

## 1. File layout

```
magic      4 bytes   "JLZ1"
version    1 byte    0x01
hdr_len    uvarint
header     hdr_len bytes of UTF-8 JSON
hdr_crc    4 bytes   CRC32 of everything above (magic through header)
block*               zero or more block frames
eof        1 byte    0x00 (a uvarint 0 where a block length would be)
```

A reader MUST verify `hdr_crc` before using any header field (a
corrupted quantum exponent would otherwise decode every block to wrong
values with all block CRCs passing), and MUST treat a missing EOF
marker as truncation, not success.

How input lines are partitioned into blocks is an encoder choice; any
partition that preserves line order is valid and decodes identically.
(The reference encoder caps blocks at 16384 lines / 32 MiB raw.)

### Header JSON

```json
{"schema": null}
{"schema": {"constants": [stream, symbol, e, s], "pexp": P, "sexp": S}}
```

`constants` are the four constant string fields of the row schema
(§3). `pexp`/`sexp` are integers in [0, 8]: prices are multiples of
`10^pexp` units of 1e-8 (e.g. `pexp = 6` means a 0.01 tick), sizes
multiples of `10^sexp` units of 1e-8. With `"schema": null` every line
of the file travels the escape channel and rows never occur.

## 2. Block frame

```
payload_len   uvarint   (> 0; the value 0 is the EOF marker)
crc           4 bytes   CRC32 of payload
payload       payload_len bytes
```

A reader MUST verify `crc` before decoding and report a corrupted
block (identified by its ordinal) on mismatch. Blocks are
self-contained: every adaptive model and every inter-row reference
(§6) starts fresh in each block, so blocks may be decoded
independently or in parallel once frames are located.

## 3. Conformant rows

A *line* is a maximal byte sequence ending with `\n` (or the file's
trailing bytes if it does not end with `\n`; such a tail is never
conformant). A line is **conformant** exactly when the renderer of §4,
applied to values extracted from it, reproduces the line byte for
byte. Everything else — malformed JSON, unknown keys, reordered keys,
whitespace variants, off-quantum prices, non-strictly-monotonic books,
out-of-range values — is an **escape line**, stored verbatim (§5.13).

A conformant row's JSON shape, with keys in exactly this order:

```
{"stream": str, "symbol": str, "event_time": str, "received_at": str,
 "payload": {"e": str, "E": int, "s": str, "U": int, "u": int,
             "b": [[price, size], ...], "a": [[price, size], ...]}}
```

with the constraints:

- `stream`, `symbol`, `e`, `s` equal the header constants.
- `E`, `U`, `u` are JSON integers in `[0, 2^62)` (and `E < 2^62/1000`).
- `event_time` is the canonical timestamp render (§4) of `E` ms — it
  is derived, never stored.
- `received_at` is a canonical timestamp render of some integer
  microsecond count in `[0, 2^62)`.
- `price`, `size` are strings matching `(0|[1-9][0-9]*)\.[0-9]{8}`
  whose value in 1e-8 units is `< 2^62` and a multiple of `10^pexp`
  (prices) or `10^sexp` (sizes). The stored integers are the quotients
  (price "ticks" and size "steps").
- bid prices strictly decrease down `b`; ask prices strictly increase
  down `a`. Either array may be empty.

## 4. Canonical renderer

A row's bytes are exactly:

```
{"stream":C0,"symbol":C1,"event_time":"T(E*1000)","received_at":"T(R)",
"payload":{"e":C2,"E":D(E),"s":C3,"U":D(U),"u":D(u),"b":L(b),"a":L(a)}}\n
```

(no spaces anywhere; shown wrapped here, the real row is one line)
where:

- `C0..C3` are the header constants serialised as JSON strings with
  Python `json.dumps` escaping: `"` and `\` backslash-escaped, control
  characters as `\uXXXX` (lowercase hex), all non-ASCII code points as
  `\uXXXX` (lowercase hex, surrogate pairs for astral code points),
  plus the shorthands `\n \t \r \b \f`.
- `D(n)` is the decimal render of a non-negative integer, no leading
  zeros, no sign.
- `T(us)` renders integer microseconds since the Unix epoch (no leap
  seconds, proleptic Gregorian) as `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`
  — zero-padded, exactly 6 fractional digits, fixed `+00:00` suffix,
  32 characters total.
- `L(levels)` renders `[]` if empty, else
  `[["P0","S0"],["P1","S1"],...]` where `Pi`/`Si` is `N(m * 10^exp)`:
  the level's stored integer times its quantum, rendered by
  `N(v8) = D(v8 div 1e8) "." zeropad8(v8 mod 1e8)` — i.e. integer
  part, dot, exactly 8 fractional digits.

## 5. Block payload

```
n_lines    uvarint    total lines in the block (rows + escapes)
n_esc      uvarint    escape-line count
gaps       n_esc x uvarint   escape line indices, delta-coded:
                      idx[0] = gaps[0]; idx[j] = idx[j-1] + gaps[j] + 1
seg_len    13 x uvarint      byte length of each segment, in §5 order
segments   concatenated segment bytes, same order
```

The payload MUST contain nothing after the last segment. Let
`n_rows = n_lines - n_esc`. Segment order and contents (every segment
is one arithmetic-coded bitstream, §6–§7):

| # | name | symbols coded |
|---|---|---|
| 1 | `e_ms` | n_rows × int: zigzag of `E - prev_E` (prev starts 0) |
| 2 | `recv` | n_rows × int: zigzag of `R - prev_R` (prev starts 0) |
| 3 | `u_gap` | n_rows × int: zigzag of `U - prev_u` (prev starts 0) |
| 4 | `u_span` | n_rows × int: zigzag of `u - U` |
| 5 | `len_b` | n_rows × int: bid level count |
| 6 | `len_a` | n_rows × int: ask level count |
| 7 | `bid_anchor` | one int per row with `len_b > 0`: zigzag of first bid tick minus previous row's first bid tick (prev starts 0) |
| 8 | `bid_delta` | `sum(max(len_b - 1, 0))` × int: `tick[i] - tick[i+1] - 1` |
| 9 | `ask_anchor` | like 7, for asks |
| 10 | `ask_delta` | like 8, with `tick[i+1] - tick[i] - 1` |
| 11 | `size_b` | `sum(len_b)` sizes, row by row, level order (§5.11) |
| 12 | `size_a` | `sum(len_a)` sizes likewise |
| 13 | `escape` | n_esc × (length int, then that many raw bytes) (§5.13) |

"prev" references advance only over conformant rows, in block order,
and reset at every block boundary.

### 5.11 Size coding

Per size value `v` (in step units): a binary symbol from the *flag*
model — 0 if `v = 0` (then nothing else), 1 otherwise, followed by the
integer `v - 1` from the segment's integer bank.

### 5.13 Escape coding

Per escape line: its byte length as an integer from the segment's
length bank (nibble layout, §6), then each raw byte split into
nibbles: the high nibble from a 16-symbol model keyed by the previous
byte's value (0 before the first byte), the low nibble from a
16-symbol model keyed by (previous byte, high nibble). Models are
lazily created.

Escape blobs are the line's exact bytes *including* its trailing `\n`;
an unterminated tail at end of file is an escape blob with no `\n`.
Concatenating a block's output therefore needs no separators.

## 6. Integer coding

Non-negative integers (zigzagged first where §5 says so) are coded by
an *integer bank*: a length model plus per-position sub-models in one
of two layouts.

- `k` = minimal number of bytes representing the value
  (`k = ceil(bitlen(z) / 8)`, with `k = 1` for `z = 0`); symbol
  `k - 1` is coded from the bank's 10-symbol length model. Encoders
  MUST use minimal `k`. Values are bounded such that `k <= 10`.
- Then the `k` bytes of `z`, most significant first, where the byte at
  position `p` (0-based from the MSB) is coded as:
  - **byte layout**: one symbol from a 256-symbol model keyed `(k, p)`;
  - **nibble layout**: the high nibble from a 16-symbol model keyed
    `(k, p)`, then the low nibble from a 16-symbol model keyed
    `(k, p, high)`.

Models are created lazily on first use; encoder and decoder
necessarily create them in the same order.

The layout per segment is fixed by this version: **nibble** for
`e_ms`, `recv`, `u_gap`, `u_span`, `len_b`, `len_a`, `bid_anchor`,
`ask_anchor` and the escape length bank; **byte** for `bid_delta`,
`ask_delta` and the size value banks. (Identical conditional entropy
by the chain rule; nibble layouts pay a smaller uniform-prior cost in
sparse contexts, byte layouts cost fewer coder operations on the
high-volume streams.)

## 7. Entropy coder

Binary-precision arithmetic coder (Witten–Neal–Cleary), 32-bit
registers: `FULL = 2^32`, `HALF`, `QUARTER`, `3QUARTER` as usual.

**State**: `low = 0`, `high = FULL - 1` (inclusive interval).

**Encode** symbol with cumulative range `[cum_lo, cum_hi)` of `total`:

```
range = high - low + 1
high  = low + floor(range * cum_hi / total) - 1
low   = low + floor(range * cum_lo / total)
loop:
  if high < HALF:            emit 0 and pending 1s
  elif low >= HALF:          emit 1 and pending 0s; low -= HALF; high -= HALF
  elif low >= QUARTER and high < 3QUARTER:
                             pending += 1; low -= QUARTER; high -= QUARTER
  else: break
  low <<= 1; high = (high << 1) | 1
```

**Finish** (per segment): `pending += 1`; emit `0` plus pending `1`s if
`low < QUARTER`, else `1` plus pending `0`s; pad the final byte with
zero bits. Bits fill bytes MSB-first.

**Decode**: initialise `code` from the segment's first 32 bits; bits
past the segment's end read as 0. The next symbol's cumulative target
is `floor(((code - low + 1) * total - 1) / range)`; find the symbol
whose `[cum_lo, cum_hi)` contains it, then update `low`/`high` as the
encoder does, mirroring the renormalisation on `code` (subtract HALF /
QUARTER alongside `low`, shifting in one new bit per iteration).

The decoder knows exactly how many symbols to read from each segment
(§5), so no terminator symbol exists.

### Adaptive models

Every model over an alphabet of `n` symbols starts with every
frequency equal to 1 (`total = n`). After coding a symbol its
frequency increases by **24**. Whenever `total` exceeds `2^16`, all
frequencies are halved rounding up (`f := (f + 1) >> 1`) and `total`
recomputed. Encoder and decoder update identically after each symbol.
`total <= 2^16` keeps the coder's interval arithmetic exact.

## 8. Decoding algorithm (per block)

1. Parse the payload header (§5); split segments by length.
2. Decode `len_b`, `len_a` (each `n_rows` symbols) — these determine
   the symbol counts of segments 7–12 — then all other segments.
3. Walk line indices `0 .. n_lines - 1`: emit the next escape blob at
   each escape index; otherwise take the next row's values, undo the
   delta/anchor references (§5), rebuild tick sequences
   (`tick[i+1] = tick[i] ∓ (delta + 1)`), and emit the canonical
   render (§4).

Concatenating all blocks' output reproduces the original file.

## 9. Versioning

The version byte after the magic identifies this specification.
Readers MUST refuse versions they do not implement. Version 1 fixes:
the schema shape of §3, the 13-segment layout of §5, the per-segment
bank layouts of §6, the coder constants of §7 (increment 24, rescale
bound 2^16, 32-bit registers), and the bounds of §6.
