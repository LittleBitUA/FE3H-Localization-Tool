"""Merge edited chunks back into translation_bundle.txt.

Input:
    project_folder/chunks/_manifest.json     (from split_bundle.py)
    project_folder/chunks_received/*.txt     (edited chunks back from translators)
                                             — falls back to project_folder/chunks/
                                               if chunks_received/ is missing.

Output:
    project_folder/translation_bundle.txt    (rewritten in place)
    project_folder/translation_bundle.txt.bak   (backup of pre-merge version)

Behavior:
    - Reads master bundle. For each BLOCK in each received chunk, looks up the
      original entry via _manifest.json by block_id, then replaces that entry's
      strings (only the indices actually present in the chunk) in the master.
    - VALIDATION:
        * Block IDs in chunks must exist in manifest.
        * Number of #N markers per block should match manifest['indices'].
          A WARNING (not error) is printed for mismatches; the merge still
          proceeds for present indices.
        * Any block in manifest NOT covered by any chunk is reported.
"""
from __future__ import annotations
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = ROOT / "project_folder" / "translation_bundle.txt"
CHUNKS_DIR = ROOT / "project_folder" / "chunks"
RECEIVED_DIR = ROOT / "project_folder" / "chunks_received"
MANIFEST_PATH = CHUNKS_DIR / "_manifest.json"

ENTRY_RE = re.compile(r"^=== ENTRY ===\s*$", re.MULTILINE)
STRING_RE = re.compile(r"^(?:#(\d+)\b|--- \[(\d+)\] ---)\s*$", re.MULTILINE)
BLOCK_RE = re.compile(r"^=== BLOCK (\d+) ===\s*$", re.MULTILINE)


def parse_bundle_entries(text: str) -> list[dict]:
    """Parse master bundle into entry dicts including header text + strings."""
    parts = ENTRY_RE.split(text)
    entries = []
    for chunk in parts[1:]:
        m = STRING_RE.search(chunk)
        if not m:
            continue
        header = chunk[: m.start()]
        body = chunk[m.start():]
        meta: dict[str, str] = {}
        comments: list[str] = []
        for line in header.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if line.lstrip().startswith("#"):
                comments.append(line)
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
        if "strings" not in meta:
            continue
        starts = list(STRING_RE.finditer(body))
        strings: dict[int, str] = {}
        for i, sm in enumerate(starts):
            idx = int(sm.group(1) or sm.group(2))
            cstart = sm.end()
            cend = starts[i + 1].start() if i + 1 < len(starts) else len(body)
            content = body[cstart:cend].strip("\n").rstrip()
            strings[idx] = content
        entries.append({
            "source": meta.get("source", ""),
            "kind": meta.get("kind", ""),
            "id": meta.get("id"),
            "path": meta.get("path"),
            "name": meta.get("name"),
            "comments": comments,
            "declared_count": int(meta["strings"]),
            "strings": strings,
            "meta_raw": meta,
        })
    return entries


def parse_chunk_blocks(text: str) -> dict[int, dict[int, str]]:
    """Parse a chunk file into {block_id: {string_index: text}}."""
    out: dict[int, dict[int, str]] = {}
    starts = list(BLOCK_RE.finditer(text))
    for i, bm in enumerate(starts):
        block_id = int(bm.group(1))
        bstart = bm.end()
        bend = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[bstart:bend]
        # Extract #N markers within this block
        smarks = list(STRING_RE.finditer(body))
        strings: dict[int, str] = {}
        for j, sm in enumerate(smarks):
            idx = int(sm.group(1) or sm.group(2))
            cstart = sm.end()
            cend = smarks[j + 1].start() if j + 1 < len(smarks) else len(body)
            content = body[cstart:cend].strip("\n").rstrip()
            strings[idx] = content
        out[block_id] = strings
    return out


def serialize_entry(entry: dict) -> str:
    """Emit one entry in the same format as the master bundle."""
    out = ["=== ENTRY ==="]
    out.append(f"source: {entry['source']}")
    out.append(f"kind: {entry['kind']}")
    if entry["source"] == "data1":
        out.append(f"id: {entry['id']}")
        if entry.get("name"):
            out.append(f"name: {entry['name']}")
    elif entry["source"] == "path":
        out.append(f"path: {entry['path']}")
    for c in entry.get("comments", []):
        out.append(c)
    out.append(f"strings: {entry['declared_count']}")
    for idx in sorted(entry["strings"].keys()):
        out.append(f"#{idx}")
        out.append(entry["strings"][idx])
        out.append("")
    return "\n".join(out) + "\n"


BUNDLE_HEADER = (
    "# FE3H UA Translation Bundle\n"
    "# ------------------------------------------------------------\n"
    "# Edit the text under each '#N' marker (legacy '--- [N] ---' also accepted).\n"
    "# Do NOT change the '=== ENTRY ===' headers — they tell the tool\n"
    "# where each block came from and how to pack it back.\n"
    "# Empty lines around strings are stripped on import.\n"
    "# ------------------------------------------------------------\n"
    "# source_lang: ENG_U\n"
    "#\n"
)


def main() -> int:
    if not BUNDLE_PATH.exists():
        print(f"ERR: master bundle missing: {BUNDLE_PATH}")
        return 1
    if not MANIFEST_PATH.exists():
        print(f"ERR: manifest missing: {MANIFEST_PATH}")
        print("Did you run split_bundle.py first?")
        return 1

    src_dir = RECEIVED_DIR if RECEIVED_DIR.exists() else CHUNKS_DIR
    print(f"Reading chunks from: {src_dir}")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    blocks_meta: dict[int, dict] = {b["block_id"]: b for b in manifest["blocks"]}
    print(f"  manifest blocks: {len(blocks_meta)}")

    # Collect translations per block from all chunks
    block_translations: dict[int, dict[int, str]] = {}
    covered_chunks: dict[int, str] = {}
    for f in sorted(src_dir.glob("chunk_*.txt")):
        chunk_blocks = parse_chunk_blocks(f.read_text(encoding="utf-8"))
        print(f"  {f.name}: {len(chunk_blocks)} blocks parsed")
        for bid, strings in chunk_blocks.items():
            if bid in block_translations:
                print(f"    WARN: block {bid} appears in multiple chunks; last one wins")
            block_translations[bid] = strings
            covered_chunks[bid] = f.name

    # Validation
    missing_from_chunks = sorted(set(blocks_meta) - set(block_translations))
    unknown_in_chunks = sorted(set(block_translations) - set(blocks_meta))
    if missing_from_chunks:
        print(f"\nWARN: {len(missing_from_chunks)} blocks from manifest not found in any chunk:")
        for b in missing_from_chunks[:5]:
            print(f"  block {b}")
        if len(missing_from_chunks) > 5:
            print(f"  ... and {len(missing_from_chunks) - 5} more")
        print("  These entries will keep their CURRENT translation in the master.")
    if unknown_in_chunks:
        print(f"\nERR: {len(unknown_in_chunks)} block IDs in chunks not in manifest "
              f"(typo? edited =BLOCK header?): {unknown_in_chunks[:10]}")
        return 2

    # Read master bundle
    master_text = BUNDLE_PATH.read_text(encoding="utf-8")
    entries = parse_bundle_entries(master_text)
    print(f"\nMaster entries: {len(entries)}")

    # Index master by (source, kind, id|path) for lookup from manifest
    def key_of(e: dict) -> str:
        if e["source"] == "data1":
            return f"data1:{e['id']}"
        if e["source"] == "path":
            return f"path:{e['path']}"
        return ""

    master_idx: dict[str, dict] = {}
    for e in entries:
        k = key_of(e)
        if k:
            master_idx[k] = e

    # STRICT VALIDATION: a block whose #N index-set differs from the manifest
    # almost always means a translator deleted/duplicated a '#N' marker and a
    # string silently glued onto its neighbour. That must be FIXED in the
    # chunk, not merged. --force downgrades this to a warning (old behavior).
    force = "--force" in sys.argv
    mismatches: list[str] = []
    for bid, strings in block_translations.items():
        meta = blocks_meta[bid]
        expected_indices = set(meta["indices"])
        got_indices = set(strings.keys())
        if expected_indices != got_indices:
            missing = sorted(expected_indices - got_indices)
            extra = sorted(got_indices - expected_indices)
            k = f"data1:{meta['id']}" if meta["source"] == "data1" else f"path:{meta['path']}"
            mismatches.append(
                f"  BLOCK {bid} ({k}) chunk={covered_chunks[bid]}: "
                f"missing #N {missing[:8]}, extra #N {extra[:8]}"
            )
    if mismatches:
        print(f"\n{'WARN' if force else 'ERR'}: {len(mismatches)} block(s) "
              f"with a broken #N marker set:")
        for m in mismatches:
            print(m)
        if not force:
            print("\nNothing was merged. Fix the markers in the chunk files")
            print("(usually a deleted '#N' line — the next string glued onto the")
            print("previous one), then re-run. Use --force to merge anyway.")
            return 2

    # Apply translations
    updated = 0
    string_changes = 0
    string_count_mismatches = len(mismatches)
    for bid, strings in block_translations.items():
        meta = blocks_meta[bid]
        if meta["source"] == "data1":
            k = f"data1:{meta['id']}"
        else:
            k = f"path:{meta['path']}"
        master_entry = master_idx.get(k)
        if master_entry is None:
            print(f"  ERR: block {bid} ({k}) not found in master bundle. Skipped.")
            continue
        for idx, new_text in strings.items():
            if idx not in master_entry["strings"]:
                # Skip indices the master doesn't have (shouldn't happen)
                continue
            if master_entry["strings"][idx] != new_text:
                master_entry["strings"][idx] = new_text
                string_changes += 1
        updated += 1

    print(f"\nBlocks updated: {updated}")
    print(f"Strings changed (vs current master): {string_changes}")
    print(f"Blocks with index-set mismatch: {string_count_mismatches}")

    # Backup + write merged master
    backup = BUNDLE_PATH.with_suffix(".txt.bak")
    shutil.copy2(BUNDLE_PATH, backup)
    print(f"\nBackup written: {backup}")

    out_lines = [BUNDLE_HEADER]
    for e in entries:
        out_lines.append("\n")
        out_lines.append(serialize_entry(e))
    BUNDLE_PATH.write_text("".join(out_lines), encoding="utf-8", newline="\n")
    print(f"Master bundle rewritten: {BUNDLE_PATH} "
          f"({BUNDLE_PATH.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
