"""TextS / Supports.bt / SC_S.bt — universal _str.bin format. UTF-8 native.

Original game files use 0xEEEEEEEE sentinel padding; some community
translations use zeros. Both must read cleanly; writer preserves padding
from source if known.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import Sequence

HEADER_SIZE = 32
HEADER_UNK1 = 1
HEADER_UNK2 = 1
HEADER_PTR_OFFSET = 0x20


@dataclass
class TextSHeader:
    unk1: int
    unk2: int
    ptr_section_offset: int
    ptr_section_size: int
    ptr_count: int
    reserved_raw: bytes      # 12 байтів after ptr_count — game uses 0xEEEEEEEE
    pad_after_ptrs: bytes    # padding bytes between offsets[count+1] і string area


@dataclass
class TextSFile:
    header: TextSHeader
    strings: list[str]
    original_bytes: bytes | None


def parse(blob: bytes) -> TextSFile:
    if len(blob) < HEADER_SIZE:
        raise ValueError(f"file too small: {len(blob)} < {HEADER_SIZE}")

    unk1, unk2, ptr_off, ptr_sz, ptr_count = struct.unpack("<5I", blob[:20])
    reserved = blob[20:32]  # 12 байтів — оригінал=0xEEEEEEEE x3; деякі community-переклади — нулі

    if unk1 != HEADER_UNK1 or unk2 != HEADER_UNK2:
        raise ValueError(f"bad TextS header: unk1={unk1} unk2={unk2}")
    if ptr_off != HEADER_PTR_OFFSET:
        raise ValueError(f"bad ptr_section_offset {ptr_off:#x}")
    if ptr_sz % 4:
        raise ValueError(f"ptr_section_size {ptr_sz} not multiple of 4")
    if HEADER_SIZE + ptr_sz > len(blob):
        raise ValueError("ptr_section overruns file")

    n_slots = ptr_sz // 4
    offsets = list(struct.unpack(f"<{n_slots}I", blob[HEADER_SIZE:HEADER_SIZE + ptr_sz]))

    # Find boundary between real offsets and trailing padding.
    # Real offsets: count+1 entries, monotonic, last == total string area size.
    real_offsets = offsets[: ptr_count + 1]
    pad_bytes_in_table = blob[HEADER_SIZE + (ptr_count + 1) * 4 : HEADER_SIZE + ptr_sz]

    str_start = ptr_off + ptr_sz
    strings: list[str] = []
    for i in range(ptr_count):
        abs_off = str_start + real_offsets[i]
        next_abs = str_start + real_offsets[i + 1]
        chunk = blob[abs_off:next_abs]
        nul = chunk.find(b"\x00")
        if nul >= 0:
            chunk = chunk[:nul]
        strings.append(chunk.decode("utf-8"))

    header = TextSHeader(
        unk1=unk1,
        unk2=unk2,
        ptr_section_offset=ptr_off,
        ptr_section_size=ptr_sz,
        ptr_count=ptr_count,
        reserved_raw=reserved,
        pad_after_ptrs=pad_bytes_in_table,
    )
    return TextSFile(header=header, strings=strings, original_bytes=blob)


def serialize(
    strings: Sequence[str],
    *,
    reserved_raw: bytes | None = None,
    pad_after_ptrs: bytes | None = None,
    pad_byte: int = 0xEE,
) -> bytes:
    """Pack a TextS file. If reserved_raw/pad_after_ptrs given (from parse), preserves
    byte-perfect round-trip with original. Otherwise uses pad_byte for sentinel."""
    count = len(strings)
    encoded = [s.encode("utf-8") + b"\x00" for s in strings]

    offsets = [0]
    for chunk in encoded:
        offsets.append(offsets[-1] + len(chunk))

    raw_ptr_size = (count + 1) * 4
    # Default: round up to multiple of 16 (matches known community patches). If we have
    # original pad_after_ptrs, preserve its exact length.
    if pad_after_ptrs is not None:
        ptr_section_size = raw_ptr_size + len(pad_after_ptrs)
        if ptr_section_size % 4:
            raise ValueError("pad_after_ptrs length breaks 4-alignment")
    else:
        ptr_section_size = ((raw_ptr_size + 15) // 16) * 16

    if reserved_raw is None:
        reserved_raw = bytes([pad_byte] * 12)
    elif len(reserved_raw) != 12:
        raise ValueError("reserved_raw must be 12 bytes")

    header = (
        struct.pack("<5I", HEADER_UNK1, HEADER_UNK2, HEADER_PTR_OFFSET, ptr_section_size, count)
        + reserved_raw
    )

    ptr_table = struct.pack(f"<{count + 1}I", *offsets)

    if pad_after_ptrs is None:
        pad_len = ptr_section_size - raw_ptr_size
        ptr_table += bytes([pad_byte] * pad_len)
    else:
        ptr_table += pad_after_ptrs

    return header + ptr_table + b"".join(encoded)


def round_trip_identical(blob: bytes) -> bool:
    """Read-then-write should be byte-identical when we preserve padding."""
    f = parse(blob)
    return (
        serialize(
            f.strings,
            reserved_raw=f.header.reserved_raw,
            pad_after_ptrs=f.header.pad_after_ptrs,
        )
        == blob
    )
