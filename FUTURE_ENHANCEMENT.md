# Future enhancements — jsonlz

Design notes for three planned extensions. Each preserves the project's two
invariants: **byte-exact round trip** and the **model/coder split** (every gain
comes from a stronger front-end model feeding the unchanged adaptive arithmetic
backend in `arith.py`). Companion to `FORMAT.md` (normative spec) and `NOTES.md`
(design log).

The three changes:

1. **JPEG-DCT front-end** — losslessly recompress baseline JPEGs by reversing
   their entropy stage and re-coding DCT coefficients through `AdaptiveModel`.
2. **Mandatory compression + encryption** — a format variant that cannot be
   written in plaintext; compress-then-AEAD with per-block integrity binding.
3. **Native hot-loop (Rust/C++)** — move the per-symbol coder loop to native
   code behind the existing Python API, validated by differential fuzzing.

---

## 1. JPEG-DCT front-end

### Principle

A baseline JPEG is already a transform-coded, entropy-coded stream. We do **not**
touch pixels. We reverse only the *entropy stage* (lossless, fully reversible) to
recover the DCT coefficients, then re-code those coefficients with a stronger
context model into the existing arithmetic backend. This is the same move jsonlz
makes on order-book rows — expose structure a byte-oriented compressor can't see —
applied to 8×8 DCT blocks. Expected gain: **~20–25% lossless** on photographic
JPEGs (the territory of Lepton / brunsli / PackJPG).

### Baseline JPEG layout (what we parse)

```
SOI · [APPn · DQT · SOF0 · DHT · DRI] (headers) · SOS · [entropy-coded scan] · EOI
```

Per 8×8 block, per component (Y, Cb, Cr, with chroma subsampling), the scan holds:

- **1 DC coefficient**, DPCM-coded (difference vs. the previous block's DC in that
  component; reset at restart markers).
- **63 AC coefficients** in zig-zag order, run-length-encoded (zero-runs) then
  Huffman-coded as `(size category, mantissa bits)`.

### Pipeline

**Encode**

1. **Parse markers.** Everything that is not the entropy-coded scan (DQT, SOF, DHT,
   APPn, DRI) is kept as a verbatim header blob and routed through a raw segment
   (the same mechanism as the existing escape channel).
2. **Huffman-decode the scan** into coefficient arrays: per block,
   `(DC_diff, [63 AC coeffs])`. This is the only decoding step and is reversible.
3. **Re-model and re-code** coefficients through `AdaptiveModel` + `arith.Encoder`.

**Decode** — inverse: decode coefficients from our streams, re-run the *original*
Huffman tables to reproduce the exact scan bytes, splice back with the header blob.

### Mapping onto existing primitives

The bolt-on is cheap because JPEG's coefficient coding already matches `codec.py`:

- **Magnitude category = the length symbol.** JPEG codes each coefficient as a
  *size category* (bit-length of |value|) followed by mantissa bits — exactly the
  `IntModels` decomposition (length symbol + per-position bits). `codec.py`'s
  integer model is, almost line-for-line, a JPEG coefficient coder.
- **Per-context sub-models = the `sub_models` dict.** Today keyed by
  `(length, position)`; for JPEG, keyed by *coefficient context*. Same
  dict-of-`AdaptiveModel` pattern, different keys — no new coder machinery.
- **Per-component, per-role streams = the existing per-field columns.**

### Stream / segment design

New block segments (added to `SEGMENT_NAMES`, FORMAT.md §6):

| segment        | content                                              |
|----------------|------------------------------------------------------|
| `jpeg_headers` | verbatim marker bytes (raw, order-1 byte model)      |
| `jpeg_dc`      | DC coefficients, per component                        |
| `jpeg_ac`      | AC coefficients, per component                        |
| `jpeg_nz`      | per-block nonzero-count / EOB symbol                  |

### Context modeling — deep dive

This is where the win lives. Add contexts **incrementally**, each justified by the
existing per-stream entropy-accounting harness (bits/symbol before vs. after) and
the KT bound, exactly as `NOTES.md` does for the order-book streams. Order of
introduction, by expected return:

**DC coefficients**

1. *Baseline:* code the DPCM diff straight through `IntModels` (zigzag the signed
   diff). Already what JPEG does — free.
2. *2D gradient prediction (biggest DC win):* predict the DC from the blocks
   **above** and **to the left**, code the residual. Context key:
   `(component, bucket(dc_left - dc_above))` — a coarse gradient bucket selects the
   model. Captures smooth luminance gradients that 1-D DPCM misses.

**AC coefficients** (the bulk of the bytes)

1. *Per-band model:* one model set per `(component, zigzag_index)`. Low-frequency
   bands are far from uniform; per-band conditioning alone is a large gain over
   JPEG's shared Huffman tables.
2. *Nonzero count / EOB:* code `num_nonzero_AC` per block first (segment
   `jpeg_nz`), then condition every AC symbol on it. Replaces JPEG's run-length
   with an explicit, model-friendly block-density signal.
3. *Same-frequency neighbor:* condition each coefficient on the same-frequency
   coefficient in the block **above** and **to the left** (bucketed). Context key:
   `(component, band, bucket(neighbor_same_freq))`. This is the Lepton-class
   context and the largest AC win; add last because it is the most state.

Each coefficient is then coded as: nonzero flag → size category (length symbol) →
mantissa bits → sign, all via `AdaptiveModel`, with the context key selecting the
sub-model. The size-category/mantissa split is precisely `IntModels` today.

### Byte-exactness — the conformance gate

Real JPEGs carry encoder-specific quirks: restart markers (`RSTn`), `0xFF00`
byte-stuffing, non-optimal Huffman tables, trailing padding bits, progressive
scans. We do **not** assume re-encoding reproduces them. Reuse jsonlz's
conformance-by-re-serialization discipline:

> After decoding coefficients and re-encoding them back to JPEG bytes, compare to
> the original. On any mismatch, **store the file (or scan segment) verbatim**
> through the raw channel.

This keeps byte-exactness *structural*, not hoped-for — identical in spirit to the
per-row `render_row(...) == line` gate in `encoder.py`.

### Scope for v1

- **In:** baseline sequential JPEG, chroma subsampling, restart intervals.
- **Out (escape wholesale for now):** progressive JPEG (multi-scan, spectral
  selection + successive approximation — needs a multi-pass coefficient model),
  arithmetic-coded JPEG (rare), 12-bit JPEG, CMYK.

### Module layout

- `jpeg_frontend.py` — marker parser + Huffman codec ↔ coefficient arrays, and the
  inverse. No coder logic of its own.
- `arith.py` / `AdaptiveModel` — unchanged; reused via context-keyed `sub_models`.
- `format.py` — new segment names; a `content_type` byte in the header (jsonl vs.
  jpeg) so the decoder dispatches the right front-end.
- A per-file re-encode-and-compare gate before committing to the compressed path.

---

## 2. Mandatory compression + encryption

### Goal

It must be impossible to produce a readable `.jlz` without encryption. Enforce this
at the **format level**, not as a CLI flag.

### Pipeline order (non-negotiable)

**Compress → then encrypt.** Encrypted data is high-entropy and won't compress;
encrypting after compression is the only correct order. Blocks are already
independent payloads — encrypt each block payload, authenticate the header and
framing.

### The one hard rule: do not roll your own crypto

The "from scratch, stdlib only" ethos is correct for the *compressor* and wrong for
the *cipher*. Hand-written AEAD is how nonce-reuse and timing bugs silently destroy
confidentiality. This is the single place to take a vetted dependency:

- **PyNaCl (libsodium)** or the **`cryptography`** package.
- AEAD: **ChaCha20-Poly1305** or **AES-256-GCM** (confidentiality + integrity).
- Python stdlib provides `hashlib.scrypt` and `hmac` (usable for key derivation)
  but **no AEAD cipher** — hence the dependency.

### Scheme

- **Key derivation:** passphrase → key via **scrypt or Argon2id**, random 16-byte
  salt. Salt + KDF params stored in the (authenticated) header. Alternatively a
  keyfile or KMS-provided key for automated archival.
- **Per-block nonce:** random per-file nonce combined with the block index (or a
  counter). **Never reuse a (key, nonce) pair.** Block independence makes this
  clean.
- **AEAD associated data binds block identity:** authenticate
  `(file_id, block_index, total_blocks)` as AAD. Critical — because blocks are
  independent, an attacker could otherwise reorder, duplicate, drop, or splice
  blocks undetectably. AAD binding makes those attacks fail the auth tag check.
- **Header authentication:** the header (magic, version, content_type, schema,
  salt, KDF params) is covered by its own AEAD/MAC so a tampered `pexp`/schema
  can't redirect decoding.

### Encrypted block-frame layout (FORMAT.md terms)

```
File:
  MAGIC "JLZE"            4 bytes        (encrypted variant — distinct from "JLZ1")
  VERSION                 1 byte
  KDF id + params         varint-framed  (e.g. scrypt N,r,p)
  salt                    16 bytes
  file_nonce              24 bytes (XChaCha20) or 12 (GCM/ChaCha20)
  header_ct_len           uvarint
  header_ciphertext       header_ct_len bytes   (compressed-side header, AEAD-sealed)
  header_tag              16 bytes              (AAD = MAGIC..file_nonce)
  [ encrypted blocks ]
  EOF marker              0x00

Encrypted block:
  block_ct_len            uvarint
  block_ciphertext        block_ct_len bytes    (AEAD of the compressed payload)
  block_tag               16 bytes
                          (nonce  = file_nonce XOR block_index)
                          (AAD    = file_id || block_index || total_blocks)
```

The inner (pre-encryption) block payload is byte-for-byte today's block payload —
the encryption layer wraps, it does not replace, the existing container.

### Enforcing the mandate

- New magic (`JLZE`) / version; the decoder rejects any file lacking the
  encryption layer. There is **no null-cipher option**.
- CLI has **no plaintext path**: `compress` requires a key source (passphrase
  prompt, `--keyfile`, or `JLZ_KEY` env) and refuses to run without one.
- `verify` decrypts → decompresses → SHA-256-compares against the *plaintext*
  original, so the byte-exact guarantee is checked end-to-end through crypto.

### Residual caveat

Keep the **CRIME/BREACH** length-leak in mind: compressing-then-encrypting leaks
plaintext information through *ciphertext length* if attacker-influenced and secret
data are co-compressed in one unit. For whole-file cold archival this is a
non-issue; if it ever becomes relevant, pad block sizes to bucket boundaries.

---

## 3. Native hot-loop (Rust / C++)

### Where the time goes

The bottleneck is the **per-symbol hot loop**: `AdaptiveModel._cum/_find/_bump`
(Fenwick walks) plus the bit-level I/O in `arith.py`, run once per coded symbol.
Tight integer/bit-twiddling is the worst case for a bytecode interpreter —
roughly **50–100× native overhead per operation**. JSON/JPEG parsing, transform
logic, and file I/O are comparatively cheap.

### Realistic expectations

- **Native coder + models:** ~3 MB/s → **order of 100–300 MB/s**. The single
  biggest available lever.
- **Still not zstd's GB/s**, and the reason is algorithmic, not language: zstd uses
  table-based **tANS/FSE + Huffman with SIMD**, far more parallel than arithmetic
  coding's inherently serial, per-symbol interval arithmetic. Native code closes
  most of the *archival* throughput gap; it does not turn arithmetic coding into
  zstd.

### Options ladder (cheapest first)

1. **PyPy** — already noted in `README.md`; ~5–15× for free, zero code change. Run
   this first to establish the real ceiling before committing to a rewrite.
2. **Hot-loop extension** — rewrite only `arith.py` + the model banks as **Rust
   (PyO3 / maturin)** or a C/Cython extension; keep orchestration, JSON/JPEG
   parsing, and format logic in Python. Best effort-to-reward ratio: the per-symbol
   cost lives in a few hundred lines.
3. **Full native rewrite** — reserve for when native throughput is the actual
   operational constraint. Trades the project's clarity goal (BRIEF: "first job is
   to be understood").

### PyO3 skeleton (option 2)

```rust
// src/lib.rs  — crate-type = ["cdylib"], maturin build
use pyo3::prelude::*;

#[pyclass]
struct AdaptiveModel { tree: Vec<u32>, n: usize, total: u32, increment: u32 }

#[pymethods]
impl AdaptiveModel {
    #[new]
    fn new(n: usize, increment: u32) -> Self { /* init Fenwick, all counts 1 */ }

    fn encode(&mut self, enc: &mut Encoder, sym: usize) {
        let (lo, hi) = (self.cum(sym), self.cum(sym + 1));
        enc.encode(lo, hi, self.total);   // interval narrow + renorm, all u32/u64
        self.bump(sym);
    }
    fn decode(&mut self, dec: &mut Decoder) -> usize { /* target → find → consume */ }
}

#[pyclass] struct Encoder { low: u32, high: u32, pending: u32, out: Vec<u8>, /*…*/ }
#[pyclass] struct Decoder { /* mirror of Encoder + code register */ }

#[pymodule]
fn jsonlz_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<AdaptiveModel>()?;
    m.add_class::<Encoder>()?;
    m.add_class::<Decoder>()?;
    Ok(())
}
```

Python imports `jsonlz_native` when present and falls back to the pure-Python
classes otherwise — the public API (`Encoder`, `Decoder`, `AdaptiveModel`) is
unchanged, so `codec.py` / `encoder.py` / `decoder.py` need no edits.

### Preserving byte-exactness across the port

A rewrite endangers the two crown jewels: **byte-exactness** and **clarity**. So:

- **Keep the Python implementation as the reference / spec oracle.** The format
  spec stays the contract; both implementations must agree bit-for-bit.
- **Differential fuzzing:** generate randomized symbol streams and assert
  `native.encode(xs) == python.encode(xs)` and that each decodes back to `xs`.
  Wire it into the existing test suite (`tests/`).
- Prefer **Rust over C++**: the byte-exact + crypto context rewards memory safety,
  and PyO3/maturin makes the hybrid path clean.

### Recommendation

PyPy to confirm the ceiling → Rust hot-loop extension behind the existing Python
API → reserve a full rewrite for when native throughput is the real constraint.
Gate the extension behind differential fuzzing against the Python reference.
