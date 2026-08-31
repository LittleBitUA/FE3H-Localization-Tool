"""INFO0 / INFO2 patcher — append/override entries in a Nintendo patch4 INFO0.

INFO0.bt v1.1:
    struct ENTRY {  // 0x120 = 288 B
        u64 entryID;
        u64 decompressed_size;
        u64 compressed_size;
        u64 is_compressed;
        char filename[256];
    }
INFO2: u64 indexed_count; u64 named_count;
"""
from __future__ import annotations
import struct
from pathlib import Path

INFO0_RECORD_SIZE = 0x120
FILENAME_SIZE = 0x100


def parse_info0(blob: bytes) -> list[dict]:
    if len(blob) % INFO0_RECORD_SIZE:
        raise ValueError(f"INFO0 size {len(blob)} not multiple of {INFO0_RECORD_SIZE}")
    count = len(blob) // INFO0_RECORD_SIZE
    out = []
    for i in range(count):
        rec = blob[i * INFO0_RECORD_SIZE:(i + 1) * INFO0_RECORD_SIZE]
        entry_id, decomp, comp, is_comp = struct.unpack("<QQQQ", rec[:0x20])
        name = rec[0x20:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        out.append({
            "entry_id": entry_id,
            "decomp_size": decomp,
            "comp_size": comp,
            "is_compressed": bool(is_comp),
            "filename": name,
        })
    return out


def serialize_info0(records: list[dict]) -> bytes:
    out = bytearray()
    for r in records:
        name = r["filename"].encode("ascii")
        if len(name) > FILENAME_SIZE - 1:
            raise ValueError(f"filename too long: {r['filename']!r}")
        name_padded = name + b"\x00" * (FILENAME_SIZE - len(name))
        out += struct.pack("<QQQQ",
            r["entry_id"],
            r["decomp_size"],
            r["comp_size"],
            1 if r["is_compressed"] else 0,
        )
        out += name_padded
    return bytes(out)


def serialize_info2(indexed_count: int, named_count: int) -> bytes:
    return struct.pack("<QQ", indexed_count, named_count)


def parse_info2(blob: bytes) -> tuple[int, int]:
    if len(blob) != 16:
        raise ValueError(f"INFO2 size {len(blob)} != 16")
    return struct.unpack("<QQ", blob)


def build_info0_overlay(
    *,
    original_info0_path: Path,
    original_info2_path: Path,
    mods_dir: Path,
) -> tuple[bytes, bytes, int]:
    """Take original patch4/INFO0 + INFO2; layer in entries for each <mods_dir>/<idx>;
    return (info0_bytes, info2_bytes, num_added).
    """
    orig = parse_info0(original_info0_path.read_bytes())
    orig_indexed, named_count = parse_info2(original_info2_path.read_bytes())
    assert len(orig) == orig_indexed, (
        f"INFO0 records ({len(orig)}) != INFO2.indexed_count ({orig_indexed})"
    )

    by_id = {r["entry_id"]: r for r in orig}
    added = 0

    if mods_dir.is_dir():
        for f in mods_dir.iterdir():
            if not f.is_file() or not f.name.isdigit():
                continue
            idx = int(f.name)
            sz = f.stat().st_size
            rec = {
                "entry_id": idx,
                "decomp_size": sz,
                "comp_size": sz,           # mods are stored uncompressed
                "is_compressed": False,
                "filename": f"rom:/mods/{idx}",
            }
            if idx not in by_id:
                added += 1
            by_id[idx] = rec

    records = [by_id[i] for i in sorted(by_id)]
    new_info0 = serialize_info0(records)
    new_info2 = serialize_info2(len(records), named_count)
    return new_info0, new_info2, added
