"""msgdata / scrdata family — 12-language container.

Layout per ThreeHousesSlave (the canonical community parser, written in C# by
marcussacana):

    FileHeader:
        u32 languages_count
        {u32 offset, u32 size}[languages_count]

    LanguageBlob (one slot):
        u32 table_count                  # constant across slots (e.g. 4)
        {u32 rel_offset, u32 size}[table_count]
        Table[table_count] at lang_start + rel_offset

    TableHeader:
        u32  magic = 0x134C58
        u16  size                        # informational
        u16  flag_size                   # number of columns
        u16  num_messages                # entries
        u16  pointer_size                # BYTES per entry row (= 4 * cols)
        u32  header_size                 # offset where the pointer array starts
        u8   flags[flag_size]            # 0=string col, 1=data col
        # alignment padding to header_size

    Pointer array at offset `header_size`:
        u32 entries[num_messages][pointer_size/4]

    String area: starts immediately after pointer array.
        Each string offset is read from a column where flag==0.
        Absolute position of a string = (pointer_value + header_size).
        0xFFFFFFFF means "empty".

Write side mirrors this: re-pack pointer rows pointing at fresh string offsets
(rel. to header_size), then append NUL-terminated UTF-8 strings.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Sequence

EMPTY = 0xFFFFFFFF


@dataclass
class TableLayout:
    rel_offset: int
    declared_size: int
    magic: int
    size_field: int
    flag_size: int
    num_messages: int
    pointer_size: int
    header_size: int
    flags: list[int]
    # pointers[entry][col] — raw u32 values from the entry row
    pointers: list[list[int]] = field(default_factory=list)
    # Flattened string column count = pointer_size / 4
    cols_per_entry: int = 0


@dataclass
class LanguageLayout:
    abs_offset: int
    size: int
    tables: list[TableLayout] = field(default_factory=list)


@dataclass
class MsgdataFile:
    languages: list[LanguageLayout]
    original_blob: bytes


def parse(blob: bytes) -> MsgdataFile:
    if len(blob) < 4:
        raise ValueError("msgdata: too small")
    n_lang = struct.unpack("<I", blob[:4])[0]
    if not (1 <= n_lang <= 32):
        raise ValueError(f"msgdata: implausible languages_count={n_lang}")

    lang_table: list[tuple[int, int]] = []
    for i in range(n_lang):
        off, sz = struct.unpack("<II", blob[4 + i * 8 : 4 + (i + 1) * 8])
        lang_table.append((off, sz))

    languages: list[LanguageLayout] = []
    for lang_off, lang_size in lang_table:
        lang_blob = blob[lang_off:lang_off + lang_size]
        layout = LanguageLayout(abs_offset=lang_off, size=lang_size, tables=[])
        if len(lang_blob) >= 4:
            n_tables = struct.unpack("<I", lang_blob[:4])[0]
            if 4 + n_tables * 8 <= len(lang_blob):
                for i in range(n_tables):
                    t_off, t_sz = struct.unpack(
                        "<II", lang_blob[4 + i * 8 : 4 + (i + 1) * 8]
                    )
                    try:
                        layout.tables.append(_parse_table(lang_blob, t_off, t_sz))
                    except Exception:
                        # Keep going so a single bad table doesn't kill the file.
                        continue
        languages.append(layout)

    return MsgdataFile(languages=languages, original_blob=blob)


def _parse_table(lang_blob: bytes, rel: int, decl_size: int) -> TableLayout:
    tbl = lang_blob[rel:rel + decl_size]
    if len(tbl) < 16:
        raise ValueError("table too short")
    magic, size_field, flag_size, num_messages, pointer_size = struct.unpack(
        "<IHHHH", tbl[:12]
    )
    header_size = struct.unpack("<I", tbl[12:16])[0]
    if magic != 0x00134C58:
        raise ValueError(f"bad table magic 0x{magic:08x}")
    if pointer_size % 4 != 0 or pointer_size == 0:
        raise ValueError(f"bad pointer_size {pointer_size}")
    if header_size < 16 or header_size > len(tbl):
        raise ValueError(f"bad header_size {header_size}")
    flags = list(tbl[16 : 16 + flag_size])

    cols_per_entry = pointer_size // 4
    # Pointer array begins at offset == header_size
    pos = header_size
    pointers: list[list[int]] = []
    row_struct = struct.Struct(f"<{cols_per_entry}I")
    for _ei in range(num_messages):
        if pos + pointer_size > len(tbl):
            break
        row = list(row_struct.unpack_from(tbl, pos))
        pointers.append(row)
        pos += pointer_size

    return TableLayout(
        rel_offset=rel,
        declared_size=decl_size,
        magic=magic,
        size_field=size_field,
        flag_size=flag_size,
        num_messages=num_messages,
        pointer_size=pointer_size,
        header_size=header_size,
        flags=flags,
        pointers=pointers,
        cols_per_entry=cols_per_entry,
    )


def _read_string_at(tbl: bytes, ptr_value: int, header_size: int) -> str | None:
    """Return None for empty (uint.MaxValue), else decoded UTF-8 string."""
    if ptr_value == EMPTY:
        return None
    pos = ptr_value + header_size
    if pos < 0 or pos >= len(tbl):
        return ""
    nul = tbl.find(b"\x00", pos)
    if nul < 0:
        nul = len(tbl)
    try:
        return tbl[pos:nul].decode("utf-8")
    except UnicodeDecodeError:
        return tbl[pos:nul].decode("utf-8", errors="replace")


def flatten_with_labels(file: MsgdataFile, slot_idx: int) -> list[tuple[str, str]]:
    """Walk strings in ThreeHousesSlave order: for each table, for each entry,
    for each column where flag==0 (string col). Empties (uint.MaxValue) are
    SKIPPED to match TextSlave's export semantics — re-pack restores them.
    """
    if not (0 <= slot_idx < len(file.languages)):
        raise ValueError(f"slot_idx {slot_idx} out of range")
    slot = file.languages[slot_idx]
    lang_blob = file.original_blob[slot.abs_offset : slot.abs_offset + slot.size]
    out: list[tuple[str, str]] = []
    for ti, tbl_layout in enumerate(slot.tables):
        tbl_bytes = lang_blob[tbl_layout.rel_offset : tbl_layout.rel_offset + tbl_layout.declared_size]
        for ei, row in enumerate(tbl_layout.pointers):
            for ci, ptr in enumerate(row):
                if ci >= len(tbl_layout.flags) or tbl_layout.flags[ci] != 0:
                    continue
                if ptr == EMPTY:
                    continue
                s = _read_string_at(tbl_bytes, ptr, tbl_layout.header_size)
                if s is None:
                    continue
                out.append((f"t{ti}.e{ei}.c{ci}", s))
    return out


def _serialize_table(tbl_layout: TableLayout, strings_iter) -> bytes:
    """Re-pack one table from new strings. `strings_iter` is an iterator yielding
    fresh text for each non-empty string column (in flatten order)."""
    # Build new header block (up to header_size) by reproducing the original
    # header bytes layout: u32 magic, u16 size, u16 flag_size, u16 num_messages,
    # u16 pointer_size, u32 header_size, u8 flags[flag_size], padding.
    out = bytearray()
    out += struct.pack(
        "<IHHHH",
        tbl_layout.magic,
        0,                        # size_field — recompute at end
        tbl_layout.flag_size,
        tbl_layout.num_messages,
        tbl_layout.pointer_size,
    )
    out += struct.pack("<I", tbl_layout.header_size)
    out += bytes(tbl_layout.flags)
    # Pad to header_size
    if len(out) < tbl_layout.header_size:
        out += b"\x00" * (tbl_layout.header_size - len(out))
    elif len(out) > tbl_layout.header_size:
        raise ValueError("header exceeds declared header_size")

    # Reserve room for pointer array.
    ptr_area_start = len(out)
    ptr_area_size = tbl_layout.num_messages * tbl_layout.pointer_size
    out += b"\x00" * ptr_area_size

    # Now append strings in walk order, recording offsets (relative to header_size).
    new_rows: list[list[int]] = []
    for row in tbl_layout.pointers:
        new_row = list(row)
        for ci, ptr in enumerate(row):
            if ci >= len(tbl_layout.flags) or tbl_layout.flags[ci] != 0:
                continue  # data col — keep raw u32
            if ptr == EMPTY:
                new_row[ci] = EMPTY
                continue
            try:
                s = next(strings_iter)
            except StopIteration:
                raise ValueError("not enough strings supplied to serialize")
            encoded = s.encode("utf-8") + b"\x00"
            string_abs = len(out)
            new_row[ci] = string_abs - tbl_layout.header_size
            out += encoded
        new_rows.append(new_row)

    # Write pointer rows.
    row_struct = struct.Struct(f"<{tbl_layout.cols_per_entry}I")
    for ei, row in enumerate(new_rows):
        # Ensure cols_per_entry length (pad with 0 if row shorter for any reason).
        if len(row) < tbl_layout.cols_per_entry:
            row = row + [0] * (tbl_layout.cols_per_entry - len(row))
        row_struct.pack_into(out, ptr_area_start + ei * tbl_layout.pointer_size, *row[:tbl_layout.cols_per_entry])

    # Patch size_field in header (table_size in u16).
    struct.pack_into("<H", out, 4, len(out) & 0xFFFF)
    return bytes(out)


def serialize_slot(slot: LanguageLayout, original_blob: bytes, new_strings: Sequence[str]) -> bytes:
    """Re-pack one language slot from fresh strings (in flatten order)."""
    lang_blob = original_blob[slot.abs_offset : slot.abs_offset + slot.size]
    strings_iter = iter(new_strings)

    n_tables = len(slot.tables)
    out = bytearray()
    out += struct.pack("<I", n_tables)
    pt_pos = len(out)
    out += b"\x00" * (n_tables * 8)

    # 4-byte align before first table (matches read side).
    pad = (-len(out)) & 3
    out += b"\x00" * pad

    new_offsets_sizes: list[tuple[int, int]] = []
    for tbl_layout in slot.tables:
        new_table = _serialize_table(tbl_layout, strings_iter)
        new_offsets_sizes.append((len(out), len(new_table)))
        out += new_table
        # 4-byte alignment between tables.
        pad = (-len(out)) & 3
        out += b"\x00" * pad

    for i, (o, s) in enumerate(new_offsets_sizes):
        struct.pack_into("<II", out, pt_pos + i * 8, o, s)

    # Ensure we consumed exactly the right number of strings.
    extra = list(strings_iter)
    if extra:
        raise ValueError(f"got {len(extra)} extra strings, expected exact match")
    return bytes(out)


def replace_language(
    file: MsgdataFile,
    target_slot: int,
    new_strings: Sequence[str],
) -> bytes:
    """Return a fresh full-file blob with one language slot replaced."""
    if not (0 <= target_slot < len(file.languages)):
        raise ValueError(f"target_slot {target_slot} out of range")

    new_slot_bytes = serialize_slot(
        file.languages[target_slot], file.original_blob, new_strings
    )

    n_lang = len(file.languages)
    out = bytearray()
    out += struct.pack("<I", n_lang)
    pt_pos = len(out)
    out += b"\x00" * (n_lang * 8)

    new_entries: list[tuple[int, int]] = []
    for i, lang in enumerate(file.languages):
        if i == target_slot:
            blob = new_slot_bytes
        else:
            blob = file.original_blob[lang.abs_offset : lang.abs_offset + lang.size]
        new_entries.append((len(out), len(blob)))
        out += blob

    for i, (o, s) in enumerate(new_entries):
        struct.pack_into("<II", out, pt_pos + i * 8, o, s)

    return bytes(out)
