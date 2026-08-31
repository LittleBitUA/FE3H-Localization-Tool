"""translation_bundle.txt text-block format: writer/parser for the
`=== ENTRY ===` / `#N` structure and its per-string framing rules."""
from __future__ import annotations


BUNDLE_HEADER = (
    "# FE3H UA Translation Bundle\n"
    "# ------------------------------------------------------------\n"
    "# Edit the text under each '#N' marker (legacy '--- [N] ---' also accepted).\n"
    "# Do NOT change the '=== ENTRY ===' headers — they tell the tool\n"
    "# where each block came from and how to pack it back.\n"
    "# Empty lines around strings are stripped on import.\n"
    "# ------------------------------------------------------------\n"
)


def _strings_to_block(strings: list[str], skip_indices: set[int] | None = None) -> str:
    """Emit string block. `skip_indices` are positions to omit (dummy entries);
    their gid is still tracked via explicit `#N` markers so re-import can
    restore them from the original blob."""
    skip = skip_indices or set()
    out = []
    for i, s in enumerate(strings):
        if i in skip:
            continue
        out.append(f"#{i}")
        out.append(s)
        out.append("")
    return "\n".join(out)


def _infer_expected_count(body: str, declared: int) -> int:
    """Determine string count for an entry. Policy: `strings: N` is the
    SOURCE OF TRUTH — it matches the original DATA1 entry layout, and the
    game/format requires this exact count. Only infer from #N markers when
    `strings:` line is missing/zero (translator deleted it). Extra markers
    beyond declared count are GARBAGE (leftover from old extracts or paste
    errors) and must be IGNORED at apply, not silently extended."""
    if declared > 0:
        return declared
    import re
    max_idx = -1
    for m in re.finditer(r"(?m)^(?:#|--- \[)(\d+)(?:\] ---)?\s*$", body):
        idx = int(m.group(1))
        if idx > max_idx:
            max_idx = idx
    return max_idx + 1 if max_idx >= 0 else 0


def _scan_max_marker(body: str) -> int:
    """Return the largest #N marker index in body, or -1 if none."""
    import re
    max_idx = -1
    for m in re.finditer(r"(?m)^(?:#|--- \[)(\d+)(?:\] ---)?\s*$", body):
        idx = int(m.group(1))
        if idx > max_idx:
            max_idx = idx
    return max_idx


def _entry_label(ordinal: int, meta: dict) -> str:
    """Human-readable label for a bundle entry, used in warning messages.
    Uses whatever identifying info is available — ordinal is always present."""
    src = meta.get("source")
    if src == "data1" and meta.get("id"):
        return f"entry #{ordinal} (source=data1, id={meta['id']})"
    if src == "path" and meta.get("path"):
        return f"entry #{ordinal} (source=path, path={meta['path']})"
    return f"entry #{ordinal}"


def _block_to_strings(block: str, expected_count: int) -> list[str]:
    import re
    # Accept BOTH new "#N" marker and legacy "--- [N] ---" so old bundles
    # still parse. New writes emit "#N" only.
    pat = re.compile(r"^(?:#|--- \[)(\d+)(?:\] ---)?\s*$", re.MULTILINE)
    matches = list(pat.finditer(block))
    result = [""] * expected_count
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        chunk = block[start:end]
        # Strip the single newline directly after "--- [N] ---" header.
        if chunk.startswith("\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith("\n"):
            chunk = chunk[1:]
        # Strip ONLY our writer-emitted blank-line separator before next marker.
        # A blank line is two consecutive line endings; strip exactly that so a
        # genuine trailing newline / trailing space inside the string is kept.
        if chunk.endswith("\r\n\r\n"):
            chunk = chunk[:-4]
        elif chunk.endswith("\n\n"):
            chunk = chunk[:-2]
        elif chunk.endswith("\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith("\n"):
            chunk = chunk[:-1]
        if 0 <= idx < expected_count:
            result[idx] = chunk
    return result


def _strip_one_newline_left(s: str) -> str:
    if s.startswith("\r\n"):
        return s[2:]
    if s.startswith("\n"):
        return s[1:]
    return s


def _strip_one_blank_separator_right(s: str) -> str:
    """Strip the blank-line separator we emit between strings.
    A blank line on disk is '\\n\\n' (Unix) or '\\r\\n\\r\\n' (Windows).
    Removing exactly that preserves any genuine trailing newline in the string."""
    if s.endswith("\r\n\r\n"):
        return s[:-4]
    if s.endswith("\n\n"):
        return s[:-2]
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n"):
        return s[:-1]
    return s


def _serialize_txt(strings: list[str]) -> str:
    out = []
    for i, s in enumerate(strings):
        out.append(f"# === [{i}] ===")
        out.append(s)
        out.append("")
    return "\n".join(out)


def _parse_txt(text: str) -> list[str]:
    import re
    pattern = re.compile(r"^# === \[(\d+)\] ===\s*$", re.MULTILINE)
    spans = []
    last_end = None
    last_idx = None
    for m in pattern.finditer(text):
        if last_idx is not None:
            spans.append((last_idx, last_end, m.start()))
        last_idx = int(m.group(1))
        last_end = m.end()
    if last_idx is not None:
        spans.append((last_idx, last_end, len(text)))
    if not spans:
        return []
    n = max(idx for idx, _, _ in spans) + 1
    result = [""] * n
    for idx, a, b in spans:
        chunk = text[a:b]
        chunk = _strip_one_newline_left(chunk)
        chunk = _strip_one_blank_separator_right(chunk)
        result[idx] = chunk
    return result


