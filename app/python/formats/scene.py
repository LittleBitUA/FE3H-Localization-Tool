"""SceneText format. Header: u32 count + {u32 off, u32 len}[count] + UTF-8 bytes.

Each string usually carries technical markers:
    [NNNN]<body text>＠NNNNNN#N
where:
    [NNNN]      — speaker / character ID (4 digits, optional)
    ＠NNNNNN    — voice line ID (fullwidth @ U+FF20, then 6 digits, optional)
    #N          — flow / choice node marker (optional)

Translators must NOT touch markers; we strip them at extract and restore from
the original blob at apply.
"""
from __future__ import annotations
import re
import struct
from dataclasses import dataclass
from typing import Sequence

_SCENE_MARKER_RE = re.compile(
    r"^(?P<prefix>\[[^\]]*\])?"               # [anything-not-]] — covers [0035] and [NULL]
    r"(?P<body>.*?)"
    r"(?P<voice>＠[\w]+)?"                # ＠NNNNNN or ＠DummyVoic (fullwidth @)
    r"(?P<flow>#\w+)?$",                      # #0 / #00 / #99
    re.DOTALL,
)


# Control-command bodies the game uses for audio / camera / scene flow;
# the player never sees them, so the translator shouldn't either.
_CONTROL_TAG_RE = re.compile(
    r"^\s*<[A-Z][A-Z0-9_ ]*>(?:\s*[\d.]+)?\s*$",
    # examples: <BGM PLAY>106, <BGM CONTINUE>, <BGM STOP>7.5,
    #           <SE PLAY>2034, <CAM>, <RIGHTS1>, <RIGHTS2>, <RIGHTS3>
)

# Developer placeholders that shipped in DATA1 (never user-facing).
# Matches whole-body strings like:
#   tTemporarymessage, iron_untranslated, weapon_untranslated_002,
#   TODO, FIXME, placeholder, lorem ipsum, DUMMY_001, ...
_DEV_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:"
    r"[\w]*_untranslated[\w]*|"            # *_untranslated*
    r"untranslated[\w]*|"                  # untranslated*
    r"tTemporarymessage|TemporaryMessage|temp[_ ]?message|"
    r"TODO|FIXME|XXX{2,}|"
    r"placeholder|PLACEHOLDER|[\w]*placeholder[\w]*|"
    r"lorem ipsum|dummy[_ ]?text|DUMMY[_]?\w*|"
    # NOTE: bare "None"/"null"/"NULL" are NOT matched here — "None" is a
    # legitimate in-game UI string (equipment/ability slots). Scene-style
    # NULL placeholders are caught by the raw-string checks in is_dummy().
    r"NotImplemented|"
    r"DEBUG[_]?\w*|[\w]*_debug|"
    r"DRAFT[_]?\w*"
    r")\s*$",
    re.IGNORECASE,
)


def is_dummy(raw: str) -> bool:
    """True iff the string is a placeholder, dev-noise, or a non-display
    control command the game executes silently."""
    # Fast raw-string heuristic: any string that carries a Dummy voice tag
    # is always a placeholder — body text irrelevant.
    if "DummyVoic" in raw or "DUMMY" in raw.upper() and "VOIC" in raw.upper():
        return True
    # Common shape `[9999]NULL#00＠…` — body becomes "NULL#00" which the
    # regex below misses, so handle it here.
    if "NULL#" in raw or raw.strip().upper().startswith("[NULL"):
        return True

    prefix, body, suffix = split_markers(raw)
    body_stripped = body.strip()

    # Developer placeholder text (whole body) — hide unconditionally
    if body_stripped and _DEV_PLACEHOLDER_RE.match(body_stripped):
        return True
    # A body that is exactly NULL (scene placeholder shape) is dev noise;
    # kept out of _DEV_PLACEHOLDER_RE so "None" remains translatable.
    if body_stripped == "NULL":
        return True
    # Pure control-command body — hide unconditionally
    if _CONTROL_TAG_RE.match(body):
        return True

    if body_stripped:
        return False

    # Empty body counts as dummy iff it carries placeholder markers
    return ("NULL" in prefix.upper() or
            "DUMMY" in suffix.upper() or
            prefix == "" and suffix == "")


def split_markers(s: str) -> tuple[str, str, str]:
    """Return (prefix, body, suffix) where suffix = voice + flow concatenated."""
    m = _SCENE_MARKER_RE.match(s)
    if not m:
        return "", s, ""
    return (m.group("prefix") or "",
            m.group("body") or "",
            (m.group("voice") or "") + (m.group("flow") or ""))


def merge_markers(prefix: str, body: str, suffix: str) -> str:
    """Inverse of split_markers — re-assemble. body may be a translation."""
    return f"{prefix}{body}{suffix}"


def reapply_markers_from_original(translated: Sequence[str],
                                  original: Sequence[str]) -> list[str]:
    """For each translated string, splice in the prefix/suffix markers from
    the matching original string. Strips any trailing whitespace from the
    body — translators often leave a stray newline at the end of a block,
    and the game's text engine treats `\\n＠NNNNNN` as malformed (which
    triggers infinite loading on Eden)."""
    out: list[str] = []
    n = max(len(translated), len(original))
    for i in range(n):
        if i >= len(translated):
            break
        tr = translated[i].rstrip()
        if i < len(original):
            prefix, _, suffix = split_markers(original[i])
        else:
            prefix, suffix = "", ""
        # If the translator left markers in place by accident, keep theirs.
        if (prefix and tr.startswith(prefix)) or (suffix and tr.endswith(suffix)):
            out.append(tr)
        else:
            out.append(merge_markers(prefix, tr, suffix))
    return out


@dataclass
class SceneFile:
    strings: list[str]
    original_blob: bytes = b""
    trailing: bytes = b""      # unknown bytes after the last string (padding)


# Max bytes allowed after the last addressed string. Anything larger means
# the "table" only covers a fraction of the file — i.e. this is NOT a
# SceneText but some other format that happens to look like one. Parsing it
# as scene and saving would TRUNCATE the file, so we refuse.
_MAX_TRAILING = 64


def parse(blob: bytes) -> SceneFile:
    if len(blob) < 4:
        raise ValueError("scene: too small")
    count = struct.unpack("<I", blob[:4])[0]
    if count == 0 or count > 0x100000:
        raise ValueError(f"scene: bad count {count}")
    table_end = 4 + count * 8
    if table_end > len(blob):
        raise ValueError("scene: table overruns file")
    strings: list[str] = []
    # Real SceneText files pack strings sequentially right after the table:
    # off[0] == table_end, and each entry is a NUL-terminated UTF-8 string
    # padded with NULs to the next offset. The table's `length` field is only
    # approximate — DATA1 entries include the NUL+padding in it, while some
    # path files UNDER-count by a byte or two (e.g. a two-digit `#00` flow
    # marker whose last digit sits past `length`). The authoritative string
    # boundary is therefore the actual NUL, bounded by the next offset.
    # Anything outside this shape is a foreign format that merely looks like
    # a scene header — parsing it and saving would truncate the file, so
    # refuse loudly.
    offsets: list[tuple[int, int]] = []
    for i in range(count):
        offsets.append(struct.unpack("<II", blob[4 + i * 8 : 4 + (i + 1) * 8]))
    if offsets[0][0] != table_end:
        raise ValueError(
            f"scene: first entry at {offsets[0][0]}, expected {table_end} — "
            f"not a SceneText file"
        )
    for i, (off, length) in enumerate(offsets):
        if i > 0 and off <= offsets[i - 1][0]:
            raise ValueError(f"scene: offsets not increasing at entry {i}")
        end_limit = offsets[i + 1][0] if i + 1 < count else len(blob)
        if off > len(blob) or end_limit > len(blob) or off >= end_limit:
            raise ValueError(f"scene: entry {i} out of bounds")
        seg = blob[off:end_limit]
        nul = seg.find(b"\x00")
        if nul < 0:
            # No terminator before the next entry: exact-fit text.
            text_bytes = seg
            tail = b""
        else:
            text_bytes = seg[:nul]
            tail = seg[nul:]
            if tail.count(b"\x00") != len(tail):
                raise ValueError(
                    f"scene: entry {i} has garbage after NUL — "
                    f"not a SceneText file"
                )
        # The table length must roughly agree with the real string length —
        # a large mismatch means this table doesn't describe this data.
        # (Real files under-count by up to ~5 bytes on long voice/flow
        # markers; impostors are off by hundreds.)
        if not (-16 <= length - len(text_bytes) <= 16):
            raise ValueError(
                f"scene: entry {i} length {length} vs actual {len(text_bytes)} — "
                f"not a SceneText file"
            )
        # Trailing padding after the LAST string doubles as the file tail —
        # cap it so a table covering a fraction of the file is rejected.
        if i + 1 == count and len(tail) > _MAX_TRAILING:
            raise ValueError(
                f"scene: {len(tail)} trailing bytes after last string — "
                f"not a SceneText file"
            )
        strings.append(text_bytes.decode("utf-8"))
    return SceneFile(strings=strings, original_blob=blob)



def serialize(strings: Sequence[str], *, original: "SceneFile | None" = None,
              force_rebuild: bool = False) -> bytes:
    """Re-pack a SceneText with NUL-terminated UTF-8 strings.

    The C# reference writer pads each entry up to a 4-byte boundary; the
    game expects offsets to align that way. Without padding Eden generates
    Unmapped Read32 errors on the next entry's header and the game freezes
    during load.
    """
    if (not force_rebuild and original and original.original_blob
            and list(strings) == original.strings):
        return original.original_blob
    count = len(strings)
    # Encode with trailing NUL, then pad each to 4-byte boundary
    encoded: list[bytes] = []
    for s in strings:
        chunk = s.encode("utf-8") + b"\x00"
        while len(chunk) % 4 != 0:
            chunk += b"\x00"
        encoded.append(chunk)
    str_start = 4 + count * 8

    table = bytearray()
    table += struct.pack("<I", count)
    cur = str_start
    for chunk in encoded:
        table += struct.pack("<II", cur, len(chunk))
        cur += len(chunk)

    # Preserve any trailing padding the original carried.
    tail = original.trailing if original else b""
    return bytes(table) + b"".join(encoded) + tail
