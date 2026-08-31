"""Caption (magic 0x2962) and Credit (magic 0x2963) formats.

Caption: per-entry {f32 start, f32 duration, char text[]}.
Credit: per-entry {char text[length_between_ptrs]} — NO timing.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Sequence

CAPTION_MAGIC = 0x00002962
CREDIT_MAGIC = 0x00002963


@dataclass
class CaptionEntry:
    start: float = 0.0
    duration: float = 0.0
    text: str = ""
    raw_chunk: bytes = b""     # original bytes between offsets (credit only)


@dataclass
class CaptionFile:
    is_credit: bool                    # True if magic 0x2963 (no timing)
    entries: list[CaptionEntry] = field(default_factory=list)
    original_blob: bytes = b""         # for byte-perfect round-trip on no-change


def parse(blob: bytes) -> CaptionFile:
    if len(blob) < 8:
        raise ValueError("caption: too small")
    magic, count = struct.unpack("<II", blob[:8])
    if magic == CAPTION_MAGIC:
        is_credit = False
    elif magic == CREDIT_MAGIC:
        is_credit = True
    else:
        raise ValueError(f"caption: bad magic 0x{magic:08x}")
    if count == 0 or count > 0x100000:
        raise ValueError(f"caption: bad count {count}")

    offs = list(struct.unpack(f"<{count}I", blob[8:8 + count * 4]))

    entries = []
    for i in range(count):
        o = offs[i]
        end = offs[i + 1] if i + 1 < count else len(blob)
        chunk = blob[o:end]
        if is_credit:
            # Raw bytes between offsets; no timing, no NUL split. Keep the
            # raw chunk so unchanged entries round-trip byte-perfectly
            # (credit files are NOT 4-byte padded, unlike captions).
            txt = chunk.rstrip(b"\x00").decode("utf-8", errors="replace")
            entries.append(CaptionEntry(0.0, 0.0, txt, raw_chunk=bytes(chunk)))
        else:
            if len(chunk) < 8:
                raise ValueError(f"caption: entry {i} too short for timing")
            start, dur = struct.unpack("<ff", chunk[:8])
            txt_chunk = chunk[8:]
            nul = txt_chunk.find(b"\x00")
            if nul >= 0:
                txt_chunk = txt_chunk[:nul]
            entries.append(
                CaptionEntry(
                    start, dur,
                    txt_chunk.decode("utf-8", errors="replace"),
                    raw_chunk=bytes(chunk),
                )
            )
    return CaptionFile(is_credit=is_credit, entries=entries, original_blob=blob)


def serialize(file: CaptionFile, new_texts: Sequence[str]) -> bytes:
    """Re-pack with new texts; preserves per-entry timing from `file.entries`."""
    if len(new_texts) != len(file.entries):
        raise ValueError(
            f"text count mismatch: have {len(file.entries)} entries, got {len(new_texts)} texts"
        )

    # No-change fast path: return original byte-for-byte (preserves padding).
    if file.original_blob and all(
        new_texts[i] == file.entries[i].text for i in range(len(new_texts))
    ):
        return file.original_blob

    count = len(new_texts)
    magic = CREDIT_MAGIC if file.is_credit else CAPTION_MAGIC

    # Layout each body, pad to 4-byte boundary (matches the C# reference
    # writer — offsets in the header MUST point to 4-byte-aligned slots;
    # without padding the game's Read32 on (start, duration) goes off
    # alignment and faults with Unmapped Read32 errors on Eden).
    bodies: list[bytes] = []
    for i, txt in enumerate(new_texts):
        if file.is_credit:
            e = file.entries[i]
            if e.raw_chunk and txt == e.text:
                # Unchanged credit entry: emit the original bytes verbatim
                # (credit files carry no 4-byte padding — do not invent it).
                bodies.append(e.raw_chunk)
                continue
            b = txt.encode("utf-8") + b"\x00"
            bodies.append(b)
            continue
        e = file.entries[i]
        timing = struct.pack("<ff", e.start, e.duration)
        if e.raw_chunk and txt == e.text and e.raw_chunk[:8] == timing:
            # Unchanged caption entry: original bytes verbatim. The game's own
            # files use irregular padding we shouldn't try to reinvent.
            bodies.append(e.raw_chunk)
            continue
        b = timing + txt.encode("utf-8") + b"\x00"
        # Pad to 4-byte boundary — caption offsets address (f32, f32) pairs
        # and MUST stay aligned.
        while len(b) % 4 != 0:
            b += b"\x00"
        bodies.append(b)

    # Header: u32 magic + u32 count + u32 offsets[count]
    header_size = 8 + count * 4
    offsets = []
    cur = header_size
    for b in bodies:
        offsets.append(cur)
        cur += len(b)

    out = bytearray()
    out += struct.pack("<II", magic, count)
    out += struct.pack(f"<{count}I", *offsets)
    for b in bodies:
        out += b
    return bytes(out)
