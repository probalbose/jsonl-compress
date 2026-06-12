"""Container framing: magic, header, block frames, varints, CRC32.

See FORMAT.md for the normative layout. Nothing here is entropy-coded;
these are the plain-byte bones of the file.
"""
import json

MAGIC = b"JLZ1"
VERSION = 1

# Fixed segment order within a block (v1). Counts/roles in FORMAT.md.
SEGMENT_NAMES = (
    "e_ms", "recv", "u_gap", "u_span", "len_b", "len_a",
    "bid_anchor", "bid_delta", "ask_anchor", "ask_delta",
    "size_b", "size_a", "escape",
)


class FormatError(Exception):
    """Structurally invalid or truncated compressed file."""


class CorruptBlockError(FormatError):
    """A block's CRC32 does not match its payload."""


def _make_crc_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _make_crc_table()


def crc32(data):
    """IEEE 802.3 CRC-32 (same polynomial/convention as zlib.crc32)."""
    crc = 0xFFFFFFFF
    table = _CRC_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def append_uvarint(buf, v):
    """LEB128 unsigned varint onto a bytearray."""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def read_uvarint_stream(f):
    v = shift = 0
    while True:
        b = f.read(1)
        if not b:
            raise FormatError("truncated varint")
        x = b[0]
        v |= (x & 0x7F) << shift
        if not x & 0x80:
            return v
        shift += 7
        if shift > 70:
            raise FormatError("varint too long")


class ByteReader:
    """Sequential reader over a block payload."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def uvarint(self):
        v = shift = 0
        data, pos = self.data, self.pos
        while True:
            if pos >= len(data):
                raise FormatError("truncated varint in block payload")
            x = data[pos]
            pos += 1
            v |= (x & 0x7F) << shift
            if not x & 0x80:
                self.pos = pos
                return v
            shift += 7
            if shift > 70:
                raise FormatError("varint too long")

    def take(self, n):
        if self.pos + n > len(self.data):
            raise FormatError("truncated block payload")
        view = memoryview(self.data)[self.pos:self.pos + n]
        self.pos += n
        return view


def write_header(fout, schema):
    """Magic, version, varint-length JSON, then CRC32 of all of that."""
    if schema is None:
        meta = {"schema": None}
    else:
        meta = {"schema": {"constants": list(schema.constants),
                           "pexp": schema.pexp, "sexp": schema.sexp}}
    blob = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    head = bytearray(MAGIC)
    head.append(VERSION)
    append_uvarint(head, len(blob))
    head += blob
    head += crc32(head).to_bytes(4, "little")
    fout.write(bytes(head))


def read_header(fin):
    """Returns the parsed header dict; verifies the header CRC32."""
    raw = bytearray(fin.read(4))
    if bytes(raw) != MAGIC:
        raise FormatError(f"bad magic {bytes(raw)!r}")
    ver = fin.read(1)
    if len(ver) != 1:
        raise FormatError("truncated header")
    if ver[0] != VERSION:
        raise FormatError(f"unsupported format version {ver[0]}")
    raw += ver
    n = shift = 0
    while True:  # uvarint, keeping the raw bytes for the CRC
        b = fin.read(1)
        if not b:
            raise FormatError("truncated header")
        raw += b
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            break
        shift += 7
        if shift > 70:
            raise FormatError("varint too long")
    blob = fin.read(n)
    if len(blob) != n:
        raise FormatError("truncated header JSON")
    raw += blob
    crc_bytes = fin.read(4)
    if len(crc_bytes) != 4:
        raise FormatError("truncated header CRC")
    if crc32(raw) != int.from_bytes(crc_bytes, "little"):
        raise CorruptBlockError("header CRC32 mismatch (corrupted header)")
    try:
        meta = json.loads(blob)
    except ValueError as exc:
        raise FormatError(f"bad header JSON: {exc}") from None
    if not isinstance(meta, dict) or "schema" not in meta:
        raise FormatError("header missing schema")
    return meta


def write_block(fout, payload):
    frame = bytearray()
    append_uvarint(frame, len(payload))
    frame += crc32(payload).to_bytes(4, "little")
    fout.write(bytes(frame))
    fout.write(payload)


def write_eof(fout):
    fout.write(b"\x00")  # uvarint 0 = end-of-blocks marker


def read_block(fin, block_index):
    """Returns payload bytes, or None at the EOF marker."""
    n = read_uvarint_stream(fin)
    if n == 0:
        return None
    crc_bytes = fin.read(4)
    if len(crc_bytes) != 4:
        raise FormatError(f"block {block_index}: truncated CRC")
    payload = fin.read(n)
    if len(payload) != n:
        raise FormatError(f"block {block_index}: truncated payload")
    if crc32(payload) != int.from_bytes(crc_bytes, "little"):
        raise CorruptBlockError(
            f"block {block_index}: CRC32 mismatch (corrupted)")
    return payload
