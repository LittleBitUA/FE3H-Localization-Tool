"""Convert FE3H translation_bundle.txt -> Weblate-ready ZIP (po/uk.po + po/en.po).

Pairs each translated string in the bundle with its original English source
extracted from data1/romfs. Output: <project>/weblate_bundle.zip with bilingual
gettext PO files (msgid=EN, msgstr=UK) under po/.
"""
from __future__ import annotations
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "python"))

from formats import texts as texts_format
from formats import scene as scene_format
from formats import caption as caption_format
from formats import msgdata as msgdata_format
from formats.data1 import iter_data0, read_entry_full

BUNDLE_PATH = ROOT / "project_folder" / "translation_bundle.txt"
ROMFS = Path(os.environ.get("FE3H_ROMFS", ""))  # set env to your dump romfs
DATA0 = ROMFS / "DATA0.bin"
DATA1 = ROMFS / "DATA1.bin"
OUT_ZIP = ROOT / "project_folder" / "weblate_bundle.zip"

ENTRY_RE = re.compile(r"^=== ENTRY ===\s*$", re.MULTILINE)
STRING_RE = re.compile(r"^(?:#(\d+)\b|--- \[(\d+)\] ---)\s*$", re.MULTILINE)


def parse_bundle(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    parts = ENTRY_RE.split(text)
    entries = []
    for chunk in parts[1:]:
        m = STRING_RE.search(chunk)
        if not m:
            continue
        header = chunk[: m.start()]
        body = chunk[m.start():]
        meta: dict[str, str] = {}
        for line in header.splitlines():
            line = line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
        if "strings" not in meta:
            continue
        # Split body by string headers, capture (index, content)
        # Use finditer to grab every header position, then slice.
        starts = list(STRING_RE.finditer(body))
        strings: dict[int, str] = {}
        for i, sm in enumerate(starts):
            idx = int(sm.group(1) or sm.group(2))
            content_start = sm.end()
            content_end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
            content = body[content_start:content_end].strip("\n")
            content = content.rstrip()
            strings[idx] = content
        entries.append({
            "source": meta.get("source", ""),
            "kind": meta.get("kind", ""),
            "id": meta.get("id"),
            "path": meta.get("path"),
            "name": meta.get("name"),
            "strings_count": int(meta["strings"]),
            "translated": strings,
        })
    return entries


def decode_original(blob: bytes, kind: str) -> list[str]:
    if kind == "texts":
        return list(texts_format.parse(blob).strings)
    if kind == "scene":
        raw = scene_format.parse(blob).strings
        return [scene_format.split_markers(s)[1] for s in raw]
    if kind in ("caption", "credit"):
        return [e.text for e in caption_format.parse(blob).entries]
    if kind == "scrdata":
        parsed = msgdata_format.parse(blob)
        return [tx for _, tx in msgdata_format.flatten_with_labels(parsed, 1)]
    raise ValueError(f"unknown kind: {kind}")


def build_data1_index(data0_path: Path):
    return {e.entry_id: e for e in iter_data0(data0_path)}


def po_escape(s: str) -> str:
    out = []
    for ch in s:
        c = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\"":
            out.append("\\\"")
        elif c == 0x09:
            out.append("\\t")
        elif c == 0x0d:
            out.append("\\r")
        elif c < 0x20 or c == 0x7f:
            out.append(f"\\{c:03o}")
        else:
            out.append(ch)
    return "".join(out)


def po_format_string(value: str) -> list[str]:
    """Return list of lines making up the quoted PO value.

    Multi-line strings get an empty leading "" and split per newline; the `\\n`
    stays at end of each line (gettext convention). Single-line gets one line.
    """
    if "\n" not in value:
        return [f"\"{po_escape(value)}\""]
    parts = value.split("\n")
    # Keep \n at end of every part except the last; last has no trailing \n
    # because we split on the real character.
    lines: list[str] = ["\"\""]
    for i, p in enumerate(parts):
        suffix = "\\n" if i < len(parts) - 1 else ""
        lines.append(f"\"{po_escape(p)}{suffix}\"")
    return lines


def write_po_block(out, msgctxt: str, comment_lines: list[str],
                    msgid: str, msgstr: str) -> None:
    for c in comment_lines:
        out.write(f"#. {c}\n")
    if msgctxt:
        out.write(f"msgctxt \"{po_escape(msgctxt)}\"\n")
    msgid_lines = po_format_string(msgid)
    if len(msgid_lines) == 1:
        out.write(f"msgid {msgid_lines[0]}\n")
    else:
        out.write("msgid " + msgid_lines[0] + "\n")
        for l in msgid_lines[1:]:
            out.write(l + "\n")
    msgstr_lines = po_format_string(msgstr)
    if len(msgstr_lines) == 1:
        out.write(f"msgstr {msgstr_lines[0]}\n\n")
    else:
        out.write("msgstr " + msgstr_lines[0] + "\n")
        for l in msgstr_lines[1:]:
            out.write(l + "\n")
        out.write("\n")


PO_HEADER_UK = (
    "msgid \"\"\n"
    "msgstr \"\"\n"
    "\"Project-Id-Version: FE3H Ukrainian Translation\\n\"\n"
    "\"Report-Msgid-Bugs-To: \\n\"\n"
    "\"MIME-Version: 1.0\\n\"\n"
    "\"Content-Type: text/plain; charset=UTF-8\\n\"\n"
    "\"Content-Transfer-Encoding: 8bit\\n\"\n"
    "\"Language: uk\\n\"\n"
    "\"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 "
    "&& n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n\"\n\n"
)

PO_HEADER_EN = (
    "msgid \"\"\n"
    "msgstr \"\"\n"
    "\"Project-Id-Version: FE3H Ukrainian Translation\\n\"\n"
    "\"Report-Msgid-Bugs-To: \\n\"\n"
    "\"MIME-Version: 1.0\\n\"\n"
    "\"Content-Type: text/plain; charset=UTF-8\\n\"\n"
    "\"Content-Transfer-Encoding: 8bit\\n\"\n"
    "\"Language: en\\n\"\n"
    "\"Plural-Forms: nplurals=2; plural=(n != 1);\\n\"\n\n"
)


def main() -> int:
    if not BUNDLE_PATH.exists():
        print(f"ERR: bundle not found: {BUNDLE_PATH}", file=sys.stderr)
        return 1
    if not DATA0.exists() or not DATA1.exists():
        print(f"ERR: missing data0/data1 at {ROMFS}", file=sys.stderr)
        return 1

    print(f"Reading bundle: {BUNDLE_PATH}")
    entries = parse_bundle(BUNDLE_PATH)
    print(f"  parsed entries: {len(entries)}")

    print(f"Indexing data1: {DATA1}")
    data1_idx = build_data1_index(DATA0)
    print(f"  data1 entry count: {len(data1_idx)}")

    uk_path = ROOT / "project_folder" / "build" / "weblate" / "po" / "uk.po"
    en_path = ROOT / "project_folder" / "build" / "weblate" / "po" / "en.po"
    uk_path.parent.mkdir(parents=True, exist_ok=True)

    pairs_total = 0
    skipped_entries = 0
    with uk_path.open("w", encoding="utf-8", newline="\n") as uk, \
         en_path.open("w", encoding="utf-8", newline="\n") as en, \
         DATA1.open("rb") as data1_f:
        uk.write(PO_HEADER_UK)
        en.write(PO_HEADER_EN)

        for ent in entries:
            src = ent["source"]
            kind = ent["kind"]
            try:
                if src == "data1":
                    eid = int(ent["id"])
                    e = data1_idx.get(eid)
                    if e is None:
                        skipped_entries += 1
                        continue
                    blob = read_entry_full(data1_f, e)
                    location_key = f"data1/{kind}/{eid}"
                    comment = [f"name: {ent['name']}"] if ent.get("name") else []
                elif src == "path":
                    rel = ent.get("path") or ""
                    full = ROMFS / rel
                    if not full.exists():
                        skipped_entries += 1
                        continue
                    blob = full.read_bytes()
                    location_key = f"path/{rel}"
                    comment = []
                else:
                    skipped_entries += 1
                    continue
                originals = decode_original(blob, kind)
            except Exception as exc:
                print(f"  skip {src}/{kind}/{ent.get('id') or ent.get('path')}: {exc}")
                skipped_entries += 1
                continue

            for idx, uk_text in ent["translated"].items():
                if idx >= len(originals):
                    continue
                en_text = originals[idx]
                msgctxt = f"{location_key}#{idx}"
                write_po_block(uk, msgctxt, comment, en_text, uk_text)
                write_po_block(en, msgctxt, comment, en_text, en_text)
                pairs_total += 1

    print(f"  total msg pairs: {pairs_total}")
    print(f"  skipped entries: {skipped_entries}")

    print(f"Writing ZIP: {OUT_ZIP}")
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(uk_path, "po/uk.po")
        zf.write(en_path, "po/en.po")
    print("Done.")
    print(f"  uk.po: {uk_path.stat().st_size:,} bytes")
    print(f"  en.po: {en_path.stat().st_size:,} bytes")
    print(f"  ZIP  : {OUT_ZIP.stat().st_size:,} bytes")
    print(f"\nWeblate component-zipfile settings:")
    print(f"  File format : gettext PO")
    print(f"  File mask   : po/*.po")
    print(f"  Source lang : en")
    return 0


if __name__ == "__main__":
    sys.exit(main())
