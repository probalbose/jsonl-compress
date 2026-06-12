"""CLI: compress <in> <out> | decompress <in> <out> | verify <orig> <restored>."""
import argparse
import hashlib
import sys

from . import FORMAT_VERSION, __version__
from .decoder import decompress
from .encoder import compress


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="jsonlz",
        description=f"jsonlz {__version__} (format v{FORMAT_VERSION}) — "
                    f"lossless JSONL compressor, stdlib only")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("compress", help="compress a JSONL file")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--block-lines", type=int, default=4096,
                   help="max lines per block (default 4096)")

    p = sub.add_parser("decompress", help="restore the original file")
    p.add_argument("input")
    p.add_argument("output")

    p = sub.add_parser("verify", help="SHA-256 compare two files")
    p.add_argument("original")
    p.add_argument("restored")

    args = parser.parse_args(argv)

    if args.cmd == "compress":
        schema, n_blocks = compress(args.input, args.output,
                                    block_lines=args.block_lines)
        tick = f"tick=1e-8*10^{schema.pexp}" if schema else "no schema (all escape)"
        print(f"compressed: {n_blocks} block(s), {tick}")
    elif args.cmd == "decompress":
        n_blocks = decompress(args.input, args.output)
        print(f"decompressed: {n_blocks} block(s)")
    else:
        a, b = sha256_file(args.original), sha256_file(args.restored)
        print(f"{a}  {args.original}")
        print(f"{b}  {args.restored}")
        if a != b:
            print("MISMATCH")
            return 1
        print("OK: byte-identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
