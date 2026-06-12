# Prompt for Claude Code (Fable 5) — paste as the opening message

I want you to build a lossless data compression tool from scratch, optimised for JSONL files, in this repository. I'm doing this partly as a learning project — I have a background in information theory and stochastic processes — and partly because the result will compress cryptocurrency order-book JSONL archives before they're shipped to cold storage. Treat me as a technically sophisticated reviewer: explain your design decisions in information-theoretic terms where relevant.

## Before writing any code

There is a representative data sample at `samples/sample_BTCUSDT.jsonl` (crypto order-book/trade records). Start by profiling it: field names and types, value distributions, timestamp and sequence-number deltas, price tick structure, per-field empirical entropy, and the zeroth-order byte entropy of the raw file. Report what you find and propose your pipeline design. Wait for my sign-off on the design before implementing.

## What "from scratch" means

- Python, standard library only. No `zlib`, `lzma`, `bz2`, `gzip` (as a codec), or any third-party compression library anywhere in the compressor or decompressor.
- The entropy coding stage must be implemented by you: an adaptive arithmetic coder (integer-arithmetic implementation with renormalisation). Huffman is acceptable only as a documented fallback if you justify it per-stream.
- The benchmark harness MAY call system `gzip` and `zstd` as comparison baselines — that's the only place external compressors appear.

## Required architecture (adapt details to what the sample profiling shows)

A transform pipeline followed by entropy coding, with each transform a documented, invertible step:
1. **Key/structure dictionary** — field names and structural boilerplate stored once in a header, not per row.
2. **Columnar transposition** — values grouped into per-field streams so each stream is statistically homogeneous.
3. **Delta encoding** for monotonic or near-monotonic numeric fields (timestamps, sequence/trade IDs) and tick-quantised price fields. Choose integer representations that make deltas small.
4. **Per-stream adaptive entropy coding** as the final stage.
5. **Escape hatch**: rows that don't fit the inferred schema (missing fields, new keys, out-of-order values, malformed lines) must be handled via an explicit exception channel, never dropped or "fixed."

## Hard requirements

- **Byte-exact round trip.** `decompress(compress(f))` must reproduce the input file byte-for-byte, verified by SHA-256. Preserve numbers as their original token text (do not round-trip through float parsing — `0.0100` must come back as `0.0100`).
- **Streaming.** Encode and decode must process files larger than memory; this machine has limited free RAM, so never load a full input into memory. Bound memory use regardless of input size.
- **Never modify anything in `samples/`.** All outputs go to `out/`. Round-trip verification writes the restored file to a separate path and compares checksums.
- **Format spec.** Write `FORMAT.md` precise enough that an independent decoder could be implemented from it alone. Include a format version byte in the header.
- **CLI**: `compress <in> <out>`, `decompress <in> <out>`, `verify <original> <restored>`.

## Acceptance criteria (measurable)

1. Byte-exact round trip on the full sample, and on adversarial inputs: empty file, single line, malformed JSON line, a line with an unseen key, a non-monotonic timestamp.
2. Property-based round-trip tests (within stdlib constraints, generate randomised JSONL inputs) plus unit tests for each transform's invertibility in isolation.
3. **Entropy accounting**: the benchmark must report, per stream, the empirical entropy of the transformed stream (bits/symbol) and the achieved coded size. The arithmetic coder must land within 5% of the measured entropy of its input stream — if it doesn't, that's a bug to find, not a number to report.
4. **Benchmark harness** comparing this tool vs `gzip -9` and `zstd` (default and `-19`) on the sample: compression ratio, encode throughput, decode throughput, and a per-stage breakdown showing how much each transform contributed. Output as a table plus a short written analysis.
5. Target to beat: better compression ratio than `gzip -9` on this JSONL sample. Report honestly against zstd; matching it is not required, but explain the gap in terms of the model/coder split.

## Working style

- Plan first, then implement in milestones; commit to git at each working milestone with a meaningful message.
- Run the tests yourself before declaring any milestone complete.
- Where the data contradicts an assumption in this prompt (e.g., a field I implied is monotonic isn't), say so and adapt — the sample is the ground truth, not my description of it.
- Keep a running `NOTES.md` of design decisions and trade-offs considered, including rate/complexity trade-offs in the modelling choices (static vs adaptive vs context models per stream) with measured justification.

## Out of scope (don't build these)

Multi-file archive handling, encryption, parallelism (note where it *would* parallelise in NOTES.md, but keep the implementation single-threaded and clear), and any GUI. Clarity and correctness beat micro-optimisation throughout — this codec's first job is to be understood, its second job is to be fast.
