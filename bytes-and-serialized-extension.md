# jsonlz: extending to raw bytes and serialized datasets

Design proposal for `~/projects/jsonl-compress`, written after reading README, BRIEF, NOTES, FORMAT, FUTURE_ENHANCEMENT and the `arith` / `codec` / `encoder` modules (as of commit `b58a5ff`). Nothing in the repo has been changed.

## Where the repo stands

- The backend is general already: a WNC arithmetic `Encoder` that takes `(cum_lo, cum_hi, total)` with `total <= 2^16`, and an `AdaptiveModel` frequency model. Nothing in `arith.py` is JSON-specific.
- The front end is not general. `transforms.py` / `encoder.py` hard-code one row grammar, the Binance `depthUpdate` schema. Every other line goes through `EscapeModels`, which is an order-1 nibble model. The README says it plainly: non-order-book input "degrades toward an order-1 byte model".
- Its most reusable idea is **conformance by re-serialization**. A row takes the modelled path only if `render(extract(x)) == x`, byte for byte; otherwise it escapes. That gate is what makes any new front end safe, whatever the format.
- `FORMAT.md` v1 is frozen and has no content-type field. `FUTURE_ENHANCEMENT.md` already plans one for JPEG.

So the extension has two halves:
- a **strong generic byte model**, for data with no structure we recognise (this replaces today's order-1 escape and serves raw binary);
- **structured front ends**, for serialized record formats, reusing the jsonlz pattern: structure dictionary, columnar streams, deltas, escape.

## Proposed architecture

```
input bytes
  -> sniff / select front end (by header, magic bytes or CLI flag)
  -> front end:  split into records -> extract (shape_id, field values) | escape
                 -> conformance gate: render(shape_id, values) == record bytes
  -> record IR:  per (shape, field path) column streams
  -> per-column transform chosen by measured code length (MDL)
  -> model banks (IntModels / SizeModels / new BitModels)
  -> unchanged arith.Encoder
```

### 1. Container v2 (prerequisite)

- New magic `JLZ2`. The header gains a `content_type` and a `frontend` params object. The v1 reader stays, so existing `.jlz` archives keep decoding. That matters given the OrderBookDynamics cold archive.
- Front-end registry: `orderbook-jsonl` (today's code, unchanged), `json-generic`, `msgpack`, `cbor`, `fixed-record`, `avro`, `protobuf`, `raw`.
- Block framing, CRCs and the per-block model resets stay exactly as they are.

### 2. Raw-bytes model (replaces the order-1 escape)

This is the hardest part to win on. zstd -19 and xz are strong on generic binary, so beating them needs context mixing, not a better order-0.

- **Binary decomposition.** Code each byte as 8 binary decisions, with the partial byte as part of the context. `Encoder.encode(lo, hi, total)` already accepts a 12-bit probability (`total = 4096`), so the coder doesn't change. Only a new `BitModel` (probability + adaptive shift-rate update) is needed.
- **Context models:** orders 0 to 4 (hashed), a sparse/word model, and a **match model**. The match model hashes the last 6 to 8 bytes, looks up the last occurrence, and predicts the byte that followed. For serialized data, with its repeated keys, class names and record layouts, the match model does most of the work.
- **Mixer:** logistic mixing (`stretch`/`squash`) with online gradient updates on the weights, then an APM/SSE stage. This is PAQ-lite (lpaq1-class), not full PAQ8.
- **Honest expectation:** roughly xz/zstd -19 parity or better on text-like and serialized data, at about 50 to 200 KB/s in pure Python. That makes the native hot loop (FUTURE_ENHANCEMENT §3) a requirement here rather than an option.

### 3. Record IR and generic JSON (the first structured front end)

- One intermediate representation for every self-describing format: a record is a `shape` (ordered field paths + type tags), and each leaf value goes to the column stream for `(shape, path)`.
- Shapes are dictionary-coded per block. Ints are zigzag-delta against the same column's previous value. Strings are coded against a per-column dictionary (MTF index), falling back to the byte model. Number tokens keep their original text via the existing "never float-parse" rule.
- **Generic JSONL schema inference** is the first user of the IR. Any JSONL, not just order books, gets the structure dictionary and columnar coding. The order-book front end stays, since it is a specialised IR with domain transforms such as tick quanta and strict book monotonicity.

### 4. Self-describing binary: MessagePack and CBOR

- Effectively binary JSON, so the same IR applies. The parser and renderer are written from scratch (stdlib only, in keeping with the project).
- The conformance gate catches non-canonical encodings, such as an int encoded wider than needed, and escapes them.

### 5. Fixed-width binary records (no schema at all)

This covers tick dumps, NumPy structured arrays, C structs, and raw column files.

- **Stride detection:** for lags k = 1..K, estimate the mutual information I(X_t ; X_{t-k}) between bytes k apart, or simply the byte-match rate. A record length shows up as a sharp peak at k = L and its multiples. Confirm by coded size on a sample.
- **Transform:** transpose into L byte columns, then try per-column candidates (raw, XOR with previous, delta of LE/BE u16/u32/u64 fields, float sign/exponent/mantissa split) and keep whichever wins under MDL (see §7).
- Detection is fully automatic; a wrong guess only costs ratio, never correctness, because the transform is invertible for any L.

### 6. Schema'd serialized formats: Avro and Protobuf

- **Avro container files** carry their schema in the header, so records parse exactly. Each field is a column, and the file's own codec block must be `null`. Already deflate/zstd-compressed Avro goes through the escape path, which is the same treatment as any pre-compressed input.
- **Protobuf** without a `.proto`: parse the wire format (tag, wire type, varint, length-delimited) and infer field paths from tags. Packed repeated fields become columns. Unparseable bytes escape.
- **Out of scope:** Parquet and ORC, which are already encoded and compressed (decoding pages is a project of its own); Java serialization and Geode PDX (possible later, since PDX is basically "type id + field data", which suits this IR); pickle (not safe to parse).

### 7. Transform selection by MDL

- Each column picks its transform and model bank by **measured code length on the block** (two-pass: try candidates, code with the best, write a small transform id). This extends the "costed under the real coding scheme" approach already used for `received_at` in NOTES.md, and the existing entropy and KT accounting extends to every new stream for free.
- This is also the "self-optimising" idea from the in-memory store discussion, applied to archives: the model chooses the encoding, and the mechanism stays deterministic and invertible.

## Milestones (each with the existing test discipline: round-trip, property tests, entropy accounting)

1. **M0 Container v2 and front-end registry.** v1 archives decode unchanged, and the order-book path is byte-identical to today.
2. **M1 Raw-bytes CM model.** BitModel, orders 0 to 4, match model, mixer, SSE. Benchmarked against gzip -9, zstd -19 and xz -9 on the Silesia corpus files plus the existing escape-heavy inputs.
3. **M2 Record IR and generic JSONL.** Benchmark on non-order-book JSONL.
4. **M3 MessagePack and CBOR front ends.**
5. **M4 Fixed-width records.** Stride detection and per-column transforms, tested on synthetic tick structs and NumPy arrays.
6. **M5 Avro and Protobuf front ends.**
7. **M6 MDL transform selection across all columns.**
8. **M7 Native hot loop** (Rust/PyO3, per FUTURE_ENHANCEMENT §3). It can move earlier if M1's throughput is too painful to iterate on.

## Questions that change the plan

1. Which serialized formats does your data actually come in (Avro, Protobuf, MessagePack, NumPy/structured binary, Java/PDX, something else)? That decides the M3 to M5 order.
2. Is a Rust dependency acceptable for M7, or should the core stay pure Python plus optional PyPy?
3. Should this be developed on a branch in the same repo (my default), keeping the jsonlz name and CLI, with `compress --type` to force a front end?
