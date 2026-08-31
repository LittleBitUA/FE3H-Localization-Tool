"""Split translation_bundle.txt into N chunks for distributed translation.

Output:
    project_folder/chunks/
        chunk_01_scrdata.txt    <- give to translator A
        chunk_02_scrdata.txt    <- give to translator B
        ...
        _manifest.json          <- internal mapping (do NOT distribute)

Each chunk hides internal IDs (source/kind/id/name) behind opaque BLOCK numbers,
but keeps the per-string '#N' markers intact so the existing edit workflow works.
The manifest lets merge_bundle.py reconstruct everything.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = ROOT / "project_folder" / "translation_bundle.txt"
OUT_DIR = ROOT / "project_folder" / "chunks"
NUM_CHUNKS = 10

# Friendly labels for the topic shown in chunk filename + header
KIND_LABEL = {
    "scrdata": "scrdata-events",
    "texts":   "texts-ui",
    "scene":   "scene-dialogues",
    "caption": "captions",
    "credit":  "credits",
}

ENTRY_RE = re.compile(r"^=== ENTRY ===\s*$", re.MULTILINE)
STRING_RE = re.compile(r"^(?:#(\d+)\b|--- \[(\d+)\] ---)\s*$", re.MULTILINE)


def parse_bundle(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    parts = ENTRY_RE.split(text)
    out = []
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
        starts = list(STRING_RE.finditer(body))
        strings: dict[int, str] = {}
        for i, sm in enumerate(starts):
            idx = int(sm.group(1) or sm.group(2))
            cstart = sm.end()
            cend = starts[i + 1].start() if i + 1 < len(starts) else len(body)
            content = body[cstart:cend].strip("\n").rstrip()
            strings[idx] = content
        out.append({
            "source": meta.get("source", ""),
            "kind": meta.get("kind", ""),
            "id": meta.get("id"),
            "path": meta.get("path"),
            "name": meta.get("name"),
            "replicates_to": meta.get("# also-replicates-to") or meta.get("also-replicates-to"),
            "declared_count": int(meta["strings"]),
            "strings": strings,
        })
    return out


def topic_of(entry: dict) -> str:
    if entry["source"] == "path":
        return "path-files"
    return KIND_LABEL.get(entry["kind"], entry["kind"] or "misc")


def split_balanced(entries_in_topic: list[dict], n_chunks: int) -> list[list[dict]]:
    """Greedy: sort entries by string count desc, place each into the currently
    smallest chunk. Produces near-balanced chunks while keeping every entry whole."""
    if n_chunks <= 0:
        return [entries_in_topic]
    chunks: list[list[dict]] = [[] for _ in range(n_chunks)]
    sizes = [0] * n_chunks
    for ent in sorted(entries_in_topic, key=lambda e: -len(e["strings"])):
        i = sizes.index(min(sizes))
        chunks[i].append(ent)
        sizes[i] += len(ent["strings"])
    return [c for c in chunks if c]


def allocate(entries: list[dict], total_chunks: int) -> dict[str, list[list[dict]]]:
    """Distribute total_chunks across topics proportional to topic size."""
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_topic[topic_of(e)].append(e)

    total = sum(len(e["strings"]) for e in entries)
    topic_size = {t: sum(len(e["strings"]) for e in es) for t, es in by_topic.items()}
    raw = {t: max(1, round(topic_size[t] / total * total_chunks)) for t in by_topic}

    # Converge to exactly total_chunks by adjusting chunks-per-topic, picking
    # the topic with the largest (or smallest) strings-per-chunk ratio.
    while sum(raw.values()) > total_chunks:
        candidates = [t for t in by_topic if raw[t] > 1]
        if not candidates:
            break
        t = min(candidates, key=lambda t: topic_size[t] / raw[t])
        raw[t] -= 1
    while sum(raw.values()) < total_chunks:
        t = max(by_topic, key=lambda t: topic_size[t] / raw[t])
        raw[t] += 1

    return {t: split_balanced(by_topic[t], raw[t]) for t in by_topic}


def render_chunk(chunk_no: int, topic: str, blocks: list[dict],
                  total_chunks: int, total_strings: int) -> tuple[str, list[dict]]:
    """Returns (file_text, manifest_entries_for_this_chunk)."""
    out_lines: list[str] = []
    out_lines.append(f"# FE3H UA Translation — Chunk {chunk_no:02d} of {total_chunks:02d}")
    out_lines.append(f"# Topic: {topic}")
    out_lines.append(f"# Blocks: {len(blocks)}   Strings to translate: {total_strings}")
    out_lines.append("# " + "-" * 64)
    out_lines.append("# HOW TO EDIT:")
    out_lines.append("#  - Translate ONLY the text under each '#N' line.")
    out_lines.append("#  - Do NOT change '=== BLOCK NNNN ===' headers.")
    out_lines.append("#  - Do NOT add or remove '#N' markers.")
    out_lines.append("#  - Keep line breaks inside a block as they are.")
    out_lines.append("#  - Empty lines around each string are stripped on import.")
    out_lines.append("# " + "-" * 64)
    out_lines.append("")

    manifest_rows: list[dict] = []
    for b in blocks:
        block_id = b["block_id"]
        out_lines.append(f"=== BLOCK {block_id:04d} ===")
        for idx in sorted(b["strings"].keys()):
            out_lines.append(f"#{idx}")
            out_lines.append(b["strings"][idx])
            out_lines.append("")
        out_lines.append("")
        manifest_rows.append({
            "block_id": block_id,
            "chunk": chunk_no,
            "topic": topic,
            "source": b["source"],
            "kind": b["kind"],
            "id": b["id"],
            "path": b["path"],
            "name": b["name"],
            "replicates_to": b["replicates_to"],
            "declared_count": b["declared_count"],
            "indices": sorted(b["strings"].keys()),
        })
    return "\n".join(out_lines), manifest_rows


def main() -> int:
    if not BUNDLE_PATH.exists():
        print(f"ERR: bundle not found: {BUNDLE_PATH}")
        return 1
    print(f"Reading: {BUNDLE_PATH}")
    entries = parse_bundle(BUNDLE_PATH)
    total_strings = sum(len(e["strings"]) for e in entries)
    print(f"  entries: {len(entries)}   strings: {total_strings}")

    # Assign a stable global BLOCK id by original bundle order
    for i, e in enumerate(entries, 1):
        e["block_id"] = i

    print(f"Allocating {NUM_CHUNKS} chunks across topics...")
    by_topic_chunks = allocate(entries, NUM_CHUNKS)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clean previous chunk_*.txt files (but keep _manifest.json comparison if user wants)
    for old in OUT_DIR.glob("chunk_*.txt"):
        old.unlink()

    chunk_no = 0
    manifest = {"chunks": [], "blocks": []}
    for topic in sorted(by_topic_chunks, key=lambda t: -sum(len(c) for c in by_topic_chunks[t])):
        for blocks in by_topic_chunks[topic]:
            chunk_no += 1
            tot = sum(len(b["strings"]) for b in blocks)
            fname = f"chunk_{chunk_no:02d}_{topic}.txt"
            text, rows = render_chunk(chunk_no, topic, blocks, NUM_CHUNKS, tot)
            (OUT_DIR / fname).write_text(text, encoding="utf-8", newline="\n")
            print(f"  {fname}: {len(blocks)} blocks, {tot} strings, "
                  f"{(OUT_DIR / fname).stat().st_size:,} bytes")
            manifest["chunks"].append({
                "chunk": chunk_no,
                "file": fname,
                "topic": topic,
                "blocks": len(blocks),
                "strings": tot,
            })
            manifest["blocks"].extend(rows)

    manifest_path = OUT_DIR / "_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nManifest: {manifest_path} ({manifest_path.stat().st_size:,} bytes)")
    print(f"Total: {chunk_no} chunks in {OUT_DIR}")
    print("Distribute chunk_*.txt files to translators.")
    print("Keep _manifest.json — needed by merge_bundle.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
