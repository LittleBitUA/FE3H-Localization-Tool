"""DATA0+DATA1 extractor. DATA0 is the master directory; DATA1 stores entry blobs.

Per Archive formats/DATA0.bt:
    struct ENTRY {  // 32 B each
        u64 offset;             // абс. offset у DATA1.bin
        u64 decompressed_size;
        u64 compressed_size;
        u64 is_compressed;
    }
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DATA0_RECORD_SIZE = 32


@dataclass
class Data0Entry:
    entry_id: int
    offset: int
    decompressed_size: int
    compressed_size: int
    is_compressed: bool


def iter_data0(data0_path: Path):
    """Yield Data0Entry for each record."""
    raw = data0_path.read_bytes()
    if len(raw) % DATA0_RECORD_SIZE:
        raise ValueError(
            f"DATA0 size {len(raw)} not multiple of {DATA0_RECORD_SIZE}"
        )
    count = len(raw) // DATA0_RECORD_SIZE
    for i in range(count):
        rec = raw[i * DATA0_RECORD_SIZE : (i + 1) * DATA0_RECORD_SIZE]
        off, decomp, comp, is_comp = struct.unpack("<QQQQ", rec)
        yield Data0Entry(
            entry_id=i,
            offset=off,
            decompressed_size=decomp,
            compressed_size=comp,
            is_compressed=bool(is_comp),
        )


def read_entry_head(f: BinaryIO, entry: Data0Entry, n: int = 32) -> bytes:
    """Read up to `n` bytes of the entry; supports both compressed and not."""
    size_on_disk = entry.compressed_size if entry.is_compressed else entry.decompressed_size
    f.seek(entry.offset)
    return f.read(min(n, size_on_disk))


def read_entry_full(f: BinaryIO, entry: Data0Entry) -> bytes:
    """Read the entire entry from DATA1, decompressing if needed."""
    size_on_disk = entry.compressed_size if entry.is_compressed else entry.decompressed_size
    f.seek(entry.offset)
    raw = f.read(size_on_disk)
    if not entry.is_compressed:
        return raw
    return decompress_koei(raw, expected_total=entry.decompressed_size)


def _align_0x80(off: int) -> int:
    return (off + 0x7F) & ~0x7F


def decompress_koei(data: bytes, expected_total: int | None = None) -> bytes:
    """Decompress Koei Tecmo chunked-zlib container (used in DATA1 + .bin.gz).

    Layout (per extractIndexNum.py from THRT):
        u32 split_size       # bytes per uncompressed chunk
        u32 num_entries      # number of chunks
        u32 total_size       # total decompressed size
        u32 splits[num_entries]  # disk size of each compressed chunk
        <align to 0x80>
        for each split:
            u32 cur_comp     # = split - 4
            bytes blob[cur_comp]  # zlib.decompress(blob)
            <align to 0x80>
        # last chunk may be stored raw if cur_comp != split - 4.
    """
    import struct
    import zlib

    if len(data) < 12:
        raise ValueError("koei .gz: too small for header")

    split_size, num_entries, total_size = struct.unpack("<III", data[:12])
    splits = list(
        struct.unpack(f"<{num_entries}I", data[12:12 + 4 * num_entries])
    )

    out = bytearray()
    off = _align_0x80(12 + 4 * num_entries)

    for i, split in enumerate(splits):
        cur_comp = struct.unpack("<I", data[off:off + 4])[0]
        if i == num_entries - 1 and cur_comp != split - 4:
            # Last chunk stored raw.
            out += data[off:off + split]
        else:
            if cur_comp != split - 4:
                raise ValueError(
                    f"koei .gz: chunk {i} size mismatch: cur_comp={cur_comp}, split={split}"
                )
            out += zlib.decompress(data[off + 4:off + 4 + cur_comp])
        off = _align_0x80(off + split)

    if expected_total is not None and len(out) != expected_total:
        raise ValueError(
            f"koei .gz: decompressed {len(out)} != expected {expected_total}"
        )
    return bytes(out)


def peek_entry_head(f: BinaryIO, entry: Data0Entry, n: int = 32) -> bytes:
    """Peek the first `n` bytes of the *decompressed* entry.

    For uncompressed entries this is one disk read; for compressed entries it
    decompresses just the first chunk (cheap relative to whole-entry decompress).
    """
    if not entry.is_compressed:
        f.seek(entry.offset)
        return f.read(min(n, entry.decompressed_size))

    # Read the chunk header to find first chunk size, then decompress only it.
    import struct
    import zlib

    f.seek(entry.offset)
    hdr = f.read(12)
    if len(hdr) < 12:
        return b""
    _split_size, num_entries, _total = struct.unpack("<III", hdr)
    if num_entries == 0:
        return b""
    splits = struct.unpack(f"<{num_entries}I", f.read(4 * num_entries))
    first_chunk_disk = splits[0]
    chunk_off = entry.offset + _align_0x80(12 + 4 * num_entries)
    f.seek(chunk_off)
    chunk = f.read(first_chunk_disk)
    cur_comp = struct.unpack("<I", chunk[:4])[0]
    if cur_comp != first_chunk_disk - 4:
        return chunk[:n]  # likely raw
    try:
        decomp = zlib.decompress(chunk[4:4 + cur_comp])
    except zlib.error:
        return b""
    return decomp[:n]
