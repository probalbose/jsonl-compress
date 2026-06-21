# jsonlz

Lossless compressor for crypto order-book JSONL archives. Pure Python,
standard library only — the entropy coder (adaptive arithmetic coding)
and every transform are implemented from scratch in this repository.
Built for archiving Binance `depthUpdate` capture files before cold
storage; any input round-trips byte-exactly, but compression gains
come from rows matching the order-book schema (see `FORMAT.md` §3).

## Usage

```sh
python3 -m jsonlz compress   capture.jsonl  capture.jlz
python3 -m jsonlz decompress capture.jlz    restored.jsonl
python3 -m jsonlz verify     capture.jsonl  restored.jsonl   # SHA-256 compare
```

`compress --block-lines N` adjusts block granularity (default 16384;
smaller blocks localise corruption more finely, larger blocks compress
slightly better — see NOTES.md).

## Guarantees

- **Byte-exact round trip** for arbitrary input. Lines that don't fit
  the schema (malformed JSON, unknown keys, off-tick prices, glued
  records, blank lines…) pass through an escape channel verbatim;
  numbers are never float-parsed.
- **Bounded memory** regardless of file size: one block (≤ 32 MiB raw)
  plus model tables, in both directions.
- **Corruption detection and localisation**: the header and every
  block carry a CRC32; a flipped bit fails decode loudly, naming the
  block, and other blocks remain decodable (blocks are
  self-contained).

## Format

`FORMAT.md` is the normative spec (frozen at version 1) — container
framing, canonical row grammar, integer decomposition, coder
constants. It is written to be sufficient for an independent decoder.
`NOTES.md` records the design decisions and the measurements behind
them.

## Benchmark

50k-line order-book captures; `tools/benchmark.py`, Python 3.13 / M-series
laptop. Full reports with per-stream entropy accounting: `out/benchmark_*.md`
(regenerate with `python3 tools/benchmark.py <sample> <report>`).

| sample | raw | jsonlz | gzip -9 | zstd | zstd -19 |
|---|---:|---:|---:|---:|---:|
| BTCUSDT (78.3 MB) | 100% | **6.27% (15.95×)** | 12.61% (7.93×) | 14.29% (7.00×) | 9.69% (10.32×) |
| XRPUSDT (24.3 MB) | 100% | **5.21% (19.18×)** | 12.15% (8.23×) | 13.74% (7.28×) | 9.11% (10.98×) |

Total coded output sits within ~1% of the measured conditional entropy
of the transformed streams (and within ±2.5% of the per-stream
Krichevsky–Trofimov sequential-coding bounds), so the remaining gap to
ideal is in the model, not the coder. The win over zstd -19 is entirely
model: field-aware transforms (structure dictionary, derived
`event_time`, columnar tick/sequence deltas) expose structure that no
byte-oriented compressor can see. Throughput is the trade: ~3 MB/s
encode/decode (pure-Python arithmetic coding) vs zstd's GB/s decode.

## Operational notes

- **Parallelise by file, not within a file.** The codec is
  single-threaded by design; archive jobs should run one process per
  capture file. (The format itself permits parallel block decode —
  blocks are independent after the header — but no implementation is
  provided. See NOTES.md.)
- **PyPy is a no-code-change speed lever.** The package is pure
  stdlib; running `pypy3 -m jsonlz …` JIT-compiles the coder's hot
  loops and typically recovers much of the interpreter overhead.
  Verify the round trip on one file after switching interpreters,
  as with any environment change.
- Compression of non-order-book JSONL still round-trips but degrades
  toward an order-1 byte model (everything escapes); check the
  reported tick on first use of a new feed — `compress` prints the
  inferred quanta.
- `verify` is cheap (two SHA-256 passes); keep it in the archive
  pipeline between compress and delete-of-original.

## Layout

```
jsonlz/        codec package (arith, codec, transforms, format,
               encoder, decoder, cli)
tests/         coder, transform, format, and round-trip suites
tools/         profile_sample.py, benchmark.py (baselines need
               system gzip/zstd; the codec itself needs nothing)
FORMAT.md      normative format spec (v1, frozen)
NOTES.md       design log with measurements
```

## OrderBookDynamics Integration

`jsonlz` is used by the OrderBookDynamics platform as a cold-archive layer for
raw Binance JSONL files.

The hot ingestion path remains unchanged:

```text
Binance REST/WebSocket -> collectors -> TimescaleDB + raw JSONL
```

Compression runs only after files are cold:

```text
raw JSONL landing -> verified .jlz archive
```

The OrderBookDynamics helper script is:

```sh
scripts/compress_jsonl_archives.sh
```

Recommended production paths on `research-host-01`:

```text
/home/probal/jsonl-compress      # this repository
/data/odb-raw/jsonl              # raw landing files
/data/archive                    # verified .jlz files
```

Recommended development paths on the MacBook:

```text
~/projects/jsonl-compress
~/OrderBookDynamicsData/data/jsonl
~/OrderBookDynamicsData/archive
```

Operational policy:

- keep raw JSONL by default;
- compress only files older than the configured cold-file age;
- verify every `.jlz` through byte-exact decompression before treating it as
  archived;
- do not delete raw files until restore and replay from `.jlz` archives are
  part of the regular validation workflow.

