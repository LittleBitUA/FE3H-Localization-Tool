"""Python sidecar: JSON-RPC over stdin/stdout (one JSON object per line)."""
from __future__ import annotations
import json
import os
import platform
import struct
import sys
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8")

from formats import texts as texts_format
from formats import scene as scene_format
from formats import caption as caption_format
from formats import msgdata as msgdata_format
from formats.data1 import iter_data0, peek_entry_head, read_entry_full, Data0Entry
from formats import info_patch
from formats.lang_detect import detect_lang_label

from appconfig import get_config

_NAMES_CACHE: dict[int, str] | None = None
# Silent translation-reference: a set of DATA1 entry IDs that an established
# community translation has already touched. Used internally as a filter to
# show only "translatable" (vs system) text. Never surfaced to the UI by name.
_TRANSLATABLE_REFERENCE_IDS: set[int] | None = None


def _try_load_translatable_reference(romfs_path: Path) -> None:
    """Load indexed entry IDs of a reference translation (configured via
    `reference_mods_dir` in fe3h-tool.config.json) as a 'these are real
    translatable entries' filter. Without config: no filter."""
    global _TRANSLATABLE_REFERENCE_IDS
    if _TRANSLATABLE_REFERENCE_IDS is not None:
        return
    ref_dir = get_config().get("reference_mods_dir")
    if ref_dir:
        mods = Path(ref_dir)
        if mods.is_dir():
            try:
                _TRANSLATABLE_REFERENCE_IDS = {
                    int(p.name) for p in mods.iterdir()
                    if p.is_file() and p.name.isdigit()
                }
                return
            except Exception:
                pass
    _TRANSLATABLE_REFERENCE_IDS = set()


# ---- DATA0 directory index (mtime-validated) ----
# Every _find_data1_entry used to be a full O(40k) reparse of DATA0.bin;
# progress/read/save called it per entry. Cache the id→Data0Entry dict.
_DATA0_INDEX_CACHE: dict[str, tuple[str, dict[int, Data0Entry]]] = {}


def _get_data0_index(data0_path: Path) -> dict[int, Data0Entry]:
    key = str(data0_path)
    st = data0_path.stat()
    sig = f"{st.st_size}-{int(st.st_mtime)}"
    cached = _DATA0_INDEX_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    index = {e.entry_id: e for e in iter_data0(data0_path)}
    _DATA0_INDEX_CACHE[key] = (sig, index)
    return index


# ---- original-blob cache (read_entry → save_entry round trip) ----
# Large originals (msgdata ≈ 9 MB) used to travel renderer-side as hex in
# `meta.original_blob_hex` (2× size through JSON-RPC + IPC, twice). Instead
# read_entry stores the blob here and hands the renderer a small `blob_key`;
# save_entry resolves it (with a re-read fallback if the sidecar restarted).
from collections import OrderedDict
_BLOB_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_BLOB_CACHE_MAX = 16


def _blob_cache_put(key: str, blob: bytes) -> None:
    _BLOB_CACHE[key] = blob
    _BLOB_CACHE.move_to_end(key)
    while len(_BLOB_CACHE) > _BLOB_CACHE_MAX:
        _BLOB_CACHE.popitem(last=False)


def _get_original_blob_for_save(params: dict, meta: dict) -> bytes:
    key = meta.get("blob_key")
    if key and key in _BLOB_CACHE:
        return _BLOB_CACHE[key]
    # Legacy bundles / old renderer builds may still send hex.
    if meta.get("original_blob_hex"):
        return bytes.fromhex(meta["original_blob_hex"])
    src = params.get("source")
    if src == "path" and params.get("orig_abs_path"):
        return Path(params["orig_abs_path"]).read_bytes()
    if src == "data1" and params.get("data0_path") and params.get("data1_path"):
        entry = _find_data1_entry(Path(params["data0_path"]), int(params["entry_id"]))
        with Path(params["data1_path"]).open("rb") as f:
            return read_entry_full(f, entry)
    raise RuntimeError(
        "original blob unavailable (sidecar restarted?) — reopen the entry and save again"
    )


def _load_names() -> dict[int, str]:
    global _NAMES_CACHE
    if _NAMES_CACHE is not None:
        return _NAMES_CACHE
    cfg_path = get_config().get("names_json")
    path = Path(cfg_path) if cfg_path else (
        Path(__file__).resolve().parent.parent.parent
        / "tools" / "references" / "filenames" / "ThreeHousesFileNames.json"
    )
    if not path.exists():
        _NAMES_CACHE = {}
        return _NAMES_CACHE
    data = json.loads(path.read_text(encoding="utf-8"))
    n2p = data.get("Num_to_Patch", {})
    _NAMES_CACHE = {int(k): v for k, v in n2p.items() if k.isdigit()}
    return _NAMES_CACHE


def _ok(req_id, result):
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)
        + "\n"
    )
    sys.stdout.flush()


def _err(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err}, ensure_ascii=False)
        + "\n"
    )
    sys.stdout.flush()


# ----- TextS detection -----

def _is_texts_head(head: bytes) -> bool:
    if len(head) < 32:
        return False
    try:
        u1, u2, po, ps, count = struct.unpack("<5I", head[:20])
    except struct.error:
        return False
    return (
        u1 == 1 and u2 == 1 and po == 0x20
        and ps % 4 == 0 and 0 < count < 0x100000
    )


def _decode_texts_sample(f, entry, head32: bytes, sample_bytes: int = 4096) -> list[str]:
    import zlib
    if not entry.is_compressed:
        f.seek(entry.offset)
        blob = f.read(min(sample_bytes, entry.decompressed_size))
    else:
        f.seek(entry.offset)
        hdr = f.read(12)
        _split, num_entries, _total = struct.unpack("<III", hdr)
        splits = struct.unpack(f"<{num_entries}I", f.read(4 * num_entries))
        align = (12 + 4 * num_entries + 0x7F) & ~0x7F
        f.seek(entry.offset + align)
        out = bytearray()
        for sz in splits:
            chunk = f.read(sz)
            cur_comp = struct.unpack("<I", chunk[:4])[0]
            try:
                out += zlib.decompress(chunk[4:4 + cur_comp])
            except zlib.error:
                break
            if len(out) >= sample_bytes:
                break
            f.read((-(f.tell() - entry.offset)) & 0x7F)
        blob = bytes(out[:sample_bytes])
    if len(blob) < 32:
        return []
    _u1, _u2, _po, ps, count = struct.unpack("<5I", blob[:20])
    if ps + 32 > len(blob) or count == 0:
        return []
    n_to_read = min(count, 3)
    try:
        offsets = struct.unpack(f"<{n_to_read + 1}I", blob[32:32 + 4 * (n_to_read + 1)])
    except struct.error:
        return []
    str_start = 32 + ps
    out_strs = []
    for i in range(n_to_read):
        a = str_start + offsets[i]
        b = str_start + offsets[i + 1] if i + 1 <= count else len(blob)
        if b > len(blob):
            break
        chunk = blob[a:b]
        nul = chunk.find(b"\x00")
        if nul >= 0:
            chunk = chunk[:nul]
        try:
            out_strs.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            out_strs.append(chunk.decode("utf-8", errors="replace"))
    return out_strs


def _classify_head(head: bytes, size: int) -> str | None:
    if len(head) < 4:
        return None
    if head[:4] == b"GT1G":
        return None
    m4 = struct.unpack("<I", head[:4])[0]
    if m4 == 0x00002962:
        return "caption"
    if m4 == 0x00002963:
        return "credit"
    for off in range(0, min(len(head) - 4, 256), 4):
        if struct.unpack("<I", head[off:off + 4])[0] == 0x134C58:
            return "scrdata"
    if _is_texts_head(head):
        return "texts"
    # SceneText layout: u32 count, then (u32 off, u32 len)[count], then UTF-8.
    # First entry's `off` must equal the post-table position (4 + count*8).
    if len(head) >= 12:
        count = struct.unpack("<I", head[:4])[0]
        expected_table_end = 4 + count * 8
        if 0 < count < 0x100000 and expected_table_end < size:
            first_off, first_len = struct.unpack("<II", head[4:12])
            if (
                first_off == expected_table_end      # first string starts at end of table
                and 0 < first_len < min(size - first_off + 1, 8192)
                and first_off + first_len <= size
            ):
                # Snip bytes available in head; decode strictly.
                snip = head[first_off : first_off + min(first_len, len(head) - first_off)]
                if snip:
                    snip = snip.rstrip(b"\x00")
                if snip:
                    try:
                        decoded = snip.decode("utf-8")
                        if any(ch.isprintable() or ch in "\n\r\t" for ch in decoded):
                            return "scene"
                    except UnicodeDecodeError:
                        pass
    return None


def _scan_lang_dir(lang_dir: Path) -> list[dict]:
    rows = []
    for f in sorted(lang_dir.iterdir()):
        if not f.is_file() or f.suffix != ".bin":
            continue
        sz = f.stat().st_size
        if sz < 32 or sz > 50_000_000:
            continue
        with f.open("rb") as h:
            head = h.read(32)
        if not _is_texts_head(head):
            continue
        count = struct.unpack("<I", head[16:20])[0]
        rows.append({
            "name": f.name,
            "abs_path": str(f),
            "size": sz,
            "string_count": count,
        })
    return rows


# ----- handlers -----

def handle_ping(_params):
    return {
        "pong": True,
        "python_version": platform.python_version(),
        "cwd": os.getcwd(),
    }


def handle_open_project(params):
    romfs = Path(params["romfs_path"])
    if not romfs.exists():
        raise ValueError(f"romfs path does not exist: {romfs}")
    data0 = romfs / "DATA0.bin"
    data1 = romfs / "DATA1.bin"
    patch4 = romfs / "patch4"
    if not (data0.exists() and data1.exists()):
        raise ValueError(
            f"Expected DATA0.bin + DATA1.bin in {romfs}. "
            f"Got DATA0={data0.exists()}, DATA1={data1.exists()}"
        )
    data1_total_entries = data0.stat().st_size // 32
    patch4_path_files = (
        sum(1 for _ in patch4.rglob("*") if _.is_file()) if patch4.is_dir() else 0
    )
    project_path = params.get("project_path")
    if project_path:
        Path(project_path).mkdir(parents=True, exist_ok=True)

    # Silent reference: load community-translated IDs to filter system noise.
    _try_load_translatable_reference(romfs)
    return {
        "romfs_path": str(romfs),
        "data0_path": str(data0),
        "data1_path": str(data1),
        "patch4_path": str(patch4) if patch4.is_dir() else None,
        "project_path": project_path,
        "data1_total_entries": data1_total_entries,
        "patch4_path_files": patch4_path_files,
        "source_lang": "ENG_U",
        "target_lang": "ENG_E",
    }


def handle_list_text_categories(params):
    """Kept for back-compat: list `patch4/nx/event/<cat>/text/<lang>/` only."""
    romfs = Path(params["romfs_path"])
    lang = params.get("lang", "ENG_U")
    categories = []
    base = romfs / "patch4" / "nx" / "event"
    if not base.is_dir():
        return categories
    for cat_dir in sorted(base.iterdir()):
        if not cat_dir.is_dir():
            continue
        text_dir = cat_dir / "text" / lang
        if not text_dir.is_dir():
            continue
        count = sum(1 for f in text_dir.iterdir() if f.is_file() and f.suffix == ".bin")
        categories.append({
            "name": cat_dir.name,
            "lang_dir": str(text_dir),
            "file_count": count,
        })
    return categories


def handle_list_texts_in_category(params):
    return _scan_lang_dir(Path(params["lang_dir"]))


def handle_survey_path_files(params):
    """Recursive scan of patch1/2/3/4 — classify EVERY .bin and yield text-bearing.

    Output: list of {abs_path, rel_path, name, kind, lang, size, string_count}.
    Result is cached to <project>/.cache/path_files_index.json (or temp dir).
    """
    import tempfile
    from formats.lang_detect import detect_from_filename
    romfs = Path(params["romfs_path"])
    project_path = params.get("project_path")
    CACHE_VERSION = 1

    if project_path:
        cache_dir = Path(project_path) / ".cache"
    else:
        cache_dir = Path(tempfile.gettempdir()) / "fe3h-ua-tool"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "path_files_index.json"
    if cache_file.exists() and not params.get("force_rescan"):
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("version") == CACHE_VERSION:
                from collections import Counter
                return {
                    "cache_path": str(cache_file),
                    "total": len(cached["entries"]),
                    "kinds": dict(Counter(r["kind"] for r in cached["entries"])),
                    "from_cache": True,
                }
        except Exception:
            pass

    rows: list[dict] = []
    for patch_name in ("patch1", "patch2", "patch3", "patch4"):
        patch_dir = romfs / patch_name
        if not patch_dir.is_dir():
            continue
        for f in patch_dir.rglob("*"):
            if not f.is_file():
                continue
            # Cheap filter by extension first.
            if f.suffix.lower() not in (".bin",):
                continue
            sz = f.stat().st_size
            if sz < 32 or sz > 50_000_000:
                continue
            try:
                with f.open("rb") as h:
                    head = h.read(256)
            except Exception:
                continue
            kind = _classify_head(head, sz)
            if kind is None:
                continue
            # Pull string_count where we know it.
            string_count = 0
            try:
                if kind == "texts":
                    string_count = struct.unpack("<I", head[16:20])[0]
                elif kind in ("caption", "credit"):
                    string_count = struct.unpack("<I", head[4:8])[0]
                elif kind == "scene":
                    string_count = struct.unpack("<I", head[:4])[0]
                elif kind == "scrdata":
                    string_count = struct.unpack("<I", head[:4])[0]
            except Exception:
                pass
            lang_label = detect_from_filename(f.name)
            rel = f.relative_to(romfs).as_posix()
            rows.append({
                "abs_path": str(f),
                "rel_path": rel,
                "name": f.name,
                "kind": kind,
                "lang": lang_label,
                "size": sz,
                "string_count": string_count,
            })

    cache_file.write_text(
        json.dumps({"version": CACHE_VERSION, "entries": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    from collections import Counter
    return {
        "cache_path": str(cache_file),
        "total": len(rows),
        "kinds": dict(Counter(r["kind"] for r in rows)),
        "from_cache": False,
    }


def handle_scan_data1_texts(params):
    data0_path = Path(params["data0_path"])
    data1_path = Path(params["data1_path"])
    project_path = params.get("project_path")
    CACHE_VERSION = 5

    import tempfile
    if project_path:
        cache_dir = Path(project_path) / ".cache"
    else:
        cache_dir = Path(tempfile.gettempdir()) / "fe3h-ua-tool"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "data1_index.json"
    if True:  # always have a cache file now
        if cache_file.exists() and not params.get("force_rescan"):
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached.get("version") == CACHE_VERSION:
                    from collections import Counter
                    kinds = Counter(r["kind"] for r in cached["entries"])
                    return {
                        "cache_path": str(cache_file),
                        "total": len(cached["entries"]),
                        "kinds": dict(kinds),
                        "from_cache": True,
                    }
            except Exception:
                pass

    names = _load_names()
    rows = []
    with data1_path.open("rb") as f:
        for e in iter_data0(data0_path):
            try:
                head = peek_entry_head(f, e, 256)
            except Exception:
                continue
            kind = _classify_head(head, e.decompressed_size)
            if kind is None:
                continue

            string_count = 0
            lang_label = "UNKNOWN"
            if kind == "texts":
                string_count = struct.unpack("<I", head[16:20])[0]
                try:
                    sample = _decode_texts_sample(f, e, head)
                    lang_label = detect_lang_label(sample)
                except Exception:
                    pass
            elif kind == "caption" or kind == "credit":
                string_count = struct.unpack("<I", head[4:8])[0]
            elif kind == "scene":
                string_count = struct.unpack("<I", head[:4])[0]
            elif kind == "scrdata":
                try:
                    string_count = struct.unpack("<I", head[:4])[0]
                except Exception:
                    pass

            rows.append({
                "entry_id": e.entry_id,
                "name": names.get(e.entry_id, f"{e.entry_id}.bin"),
                "decomp_size": e.decompressed_size,
                "compressed": e.is_compressed,
                "string_count": string_count,
                "kind": kind,
                "lang": lang_label,
            })

    # Silent filter: keep only entries known to carry translatable (non-system)
    # text. If we have no reference, leave everything.
    if _TRANSLATABLE_REFERENCE_IDS:
        rows = [r for r in rows if r["entry_id"] in _TRANSLATABLE_REFERENCE_IDS]

    cache_file.write_text(
        json.dumps({"version": CACHE_VERSION, "entries": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    from collections import Counter
    return {
        "cache_path": str(cache_file),
        "total": len(rows),
        "kinds": dict(Counter(r["kind"] for r in rows)),
        "from_cache": False,
    }


def _find_data1_entry(data0_path: Path, entry_id: int) -> Data0Entry:
    e = _get_data0_index(data0_path).get(entry_id)
    if e is None:
        raise ValueError(f"entry_id {entry_id} not found")
    return e


def _load_blob(params: dict) -> bytes:
    """Resolve source=path|data1 → raw decompressed bytes."""
    src = params["source"]
    if src == "path":
        return Path(params["abs_path"]).read_bytes()
    if src == "data1":
        entry = _find_data1_entry(Path(params["data0_path"]), int(params["entry_id"]))
        with Path(params["data1_path"]).open("rb") as f:
            return read_entry_full(f, entry)
    raise ValueError(f"unknown source: {src}")


def _blob_key_of(params: dict) -> str:
    if params["source"] == "path":
        return f"path:{params['abs_path']}"
    return f"data1:{params['data1_path']}:{int(params['entry_id'])}"


def handle_read_entry(params):
    kind = params["kind"]
    blob = _load_blob(params)
    blob_key = _blob_key_of(params)
    _blob_cache_put(blob_key, blob)

    if kind == "texts":
        parsed = texts_format.parse(blob)
        return {
            "strings": parsed.strings,
            "meta": {
                "kind": "texts",
                "reserved_raw_hex": parsed.header.reserved_raw.hex(),
                "pad_after_ptrs_hex": parsed.header.pad_after_ptrs.hex(),
            },
        }
    if kind == "scene":
        parsed = scene_format.parse(blob)
        return {
            "strings": parsed.strings,
            "meta": {"kind": "scene", "blob_key": blob_key},
        }
    if kind == "caption" or kind == "credit":
        parsed = caption_format.parse(blob)
        return {
            "strings": [e.text for e in parsed.entries],
            "timings": [
                {"start": e.start, "duration": e.duration} for e in parsed.entries
            ],
            "meta": {
                "kind": kind,
                "is_credit": parsed.is_credit,
                "blob_key": blob_key,
            },
        }
    if kind == "scrdata":
        parsed = msgdata_format.parse(blob)
        # Source slot = 1 (ENG_U). Target slot = 2 (ENG_E) unless overridden.
        labels_and_texts = msgdata_format.flatten_with_labels(parsed, 1)
        labels = [lb for lb, _ in labels_and_texts]
        texts = [tx for _, tx in labels_and_texts]
        return {
            "strings": texts,
            "meta": {
                "kind": "scrdata",
                "labels": labels,
                "target_slot": 2,
                "blob_key": blob_key,
                "num_languages": len(parsed.languages),
                "tables_in_source_slot": len(parsed.languages[1].tables),
            },
        }
    raise ValueError(f"read_entry: unsupported kind '{kind}'")


def _resolve_save_path(params: dict) -> Path:
    project_path = Path(params["project_path"]).resolve()
    src = params["source"]
    if src == "path":
        rel = params["rel_from_romfs"].replace("\\", "/")
        if Path(rel).is_absolute() or rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError(f"unsafe rel: {rel!r}")
        p = (project_path / "romfs" / rel).resolve()
    elif src == "data1":
        p = (project_path / "romfs" / "mods" / str(int(params["entry_id"]))).resolve()
    else:
        raise ValueError(f"unknown source: {src}")
    if not str(p).startswith(str(project_path)):
        raise ValueError(f"refusing to write outside project: {p}")
    return p


def handle_save_entry(params):
    kind = params["kind"]
    strings = params["strings"]
    meta = params.get("meta") or {}
    out_path = _resolve_save_path(params)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if kind == "texts":
        blob = texts_format.serialize(
            strings,
            reserved_raw=bytes.fromhex(meta["reserved_raw_hex"]) if meta.get("reserved_raw_hex") else None,
            pad_after_ptrs=bytes.fromhex(meta["pad_after_ptrs_hex"]) if meta.get("pad_after_ptrs_hex") else None,
        )
    elif kind == "scene":
        original_blob = _get_original_blob_for_save(params, meta)
        original_parsed = scene_format.parse(original_blob)
        blob = scene_format.serialize(strings, original=original_parsed)
    elif kind in ("caption", "credit"):
        original_blob = _get_original_blob_for_save(params, meta)
        original_parsed = caption_format.parse(original_blob)
        if params.get("timings"):
            for i, t in enumerate(params["timings"]):
                if i >= len(original_parsed.entries):
                    break
                original_parsed.entries[i].start = float(t["start"])
                original_parsed.entries[i].duration = float(t["duration"])
        blob = caption_format.serialize(original_parsed, strings)
    elif kind == "scrdata":
        original_blob = _get_original_blob_for_save(params, meta)
        parsed = msgdata_format.parse(original_blob)
        target_slot = int(params.get("target_slot", meta.get("target_slot", 2)))
        blob = msgdata_format.replace_language(parsed, target_slot, strings)
    else:
        raise ValueError(f"save_entry: unsupported kind '{kind}'")

    existed = out_path.exists()
    original_on_disk = out_path.read_bytes() if existed else b""
    out_path.write_bytes(blob)
    return {
        "path": str(out_path),
        "bytes_written": len(blob),
        "identical_to_original": existed and blob == original_on_disk,
        "is_new": not existed,
    }


def handle_read_existing_translation_unified(params):
    project_path = Path(params["project_path"])
    src = params["source"]
    if src == "path":
        rel = params["rel_from_romfs"].replace("\\", "/")
        path = project_path / "romfs" / rel
    else:
        path = project_path / "romfs" / "mods" / str(int(params["entry_id"]))
    if not path.exists():
        return {"exists": False, "strings": []}
    try:
        blob = path.read_bytes()
    except Exception:
        return {"exists": False, "strings": []}

    # When the caller tells us the kind, parse it directly — this is the
    # only correct route for scrdata (the probe loop below can't know which
    # language slot holds the translation).
    kind = params.get("kind")
    target_slot = int(params.get("target_slot", 2))
    if kind:
        try:
            if kind == "texts":
                return {"exists": True, "strings": texts_format.parse(blob).strings}
            if kind == "scene":
                return {"exists": True, "strings": scene_format.parse(blob).strings}
            if kind in ("caption", "credit"):
                parsed = caption_format.parse(blob)
                return {"exists": True, "strings": [e.text for e in parsed.entries]}
            if kind == "scrdata":
                parsed = msgdata_format.parse(blob)
                strings = [t for _, t in msgdata_format.flatten_with_labels(parsed, target_slot)]
                return {"exists": True, "strings": strings}
        except Exception:
            return {"exists": False, "strings": []}

    # Legacy probe (no kind hint): try formats in order.
    for fmt_kind in ("texts", "scene", "caption", "credit", "scrdata"):
        try:
            if fmt_kind == "texts":
                parsed = texts_format.parse(blob)
                return {"exists": True, "strings": parsed.strings}
            if fmt_kind == "scene":
                parsed = scene_format.parse(blob)
                return {"exists": True, "strings": parsed.strings}
            if fmt_kind in ("caption", "credit"):
                parsed = caption_format.parse(blob)
                return {"exists": True, "strings": [e.text for e in parsed.entries]}
            if fmt_kind == "scrdata":
                parsed = msgdata_format.parse(blob)
                strings = [t for _, t in msgdata_format.flatten_with_labels(parsed, target_slot)]
                return {"exists": True, "strings": strings}
        except Exception:
            continue
    return {"exists": False, "strings": []}


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


def handle_extract_all_texts(params):
    """Produce a single translation bundle TXT for the translator.

    Output: <project>/translation_bundle.txt
    Format per entry:
        === ENTRY ===
        source: data1|path
        kind:   texts|scene|caption|credit|scrdata
        id:     <entry_id>           (for data1)
        path:   <rel/from/romfs>     (for path)
        strings: <N>
        --- [0] ---
        <text>

        --- [1] ---
        <text>
    """
    from formats.lang_detect import detect_from_filename, SOURCE_TO_LABEL
    project_path = Path(params["project_path"]).resolve()
    data0_path = Path(params["data0_path"])
    data1_path = Path(params["data1_path"])
    romfs_path = Path(params["romfs_path"])
    source_lang = params.get("source_lang", "ENG_U")
    desired_label = SOURCE_TO_LABEL.get(source_lang.upper(), "ENG")

    project_path.mkdir(parents=True, exist_ok=True)
    out_file = project_path / "translation_bundle.txt"
    names = _load_names()
    _try_load_translatable_reference(romfs_path)

    # MERGE MODE: load any existing bundle.txt and keep the translator's
    # strings for entries we re-encounter. New entries take their original
    # strings as a starting point. Existing translations are NEVER lost.
    existing_translations: dict[str, list[str]] = {}
    if out_file.exists():
        import re as _re_merge
        try:
            old_text = out_file.read_text(encoding="utf-8")
            for chunk in _re_merge.split(r"(?m)^=== ENTRY ===\n", old_text)[1:]:
                hm = _re_merge.search(r"^(?:#\d+\b|--- \[\d+\] ---)", chunk, _re_merge.MULTILINE)
                if not hm:
                    continue
                hdr = chunk[:hm.start()]
                body = chunk[hm.start():]
                meta_old: dict[str, str] = {}
                for line in hdr.splitlines():
                    if ":" in line and not line.lstrip().startswith("#"):
                        k, _, v = line.partition(":")
                        meta_old[k.strip().lower()] = v.strip()
                try:
                    count = int(meta_old.get("strings", "0"))
                except Exception:
                    continue
                if count <= 0:
                    continue
                src_old = meta_old.get("source")
                if src_old == "data1":
                    key = f"data1:{meta_old.get('id')}"
                elif src_old == "path":
                    key = f"path:{meta_old.get('path')}"
                else:
                    continue
                try:
                    existing_translations[key] = _block_to_strings(body, count)
                except Exception:
                    pass
            # Make a side backup just in case (small disk cost, big peace of mind)
            backup = out_file.with_suffix(".txt.bak")
            backup.write_bytes(out_file.read_bytes())
        except Exception:
            existing_translations = {}

    def _strings_of(blob: bytes, kind: str) -> tuple[list[str], set[int]] | None:
        """Return (strings_for_bundle, dummy_indices_to_skip)."""
        try:
            if kind == "texts":
                ss = texts_format.parse(blob).strings
                d = {i for i, s in enumerate(ss) if scene_format.is_dummy(s)}
                return ss, d
            if kind == "scene":
                raw = scene_format.parse(blob).strings
                bodies = [scene_format.split_markers(s)[1] for s in raw]
                dummies = {i for i, s in enumerate(raw) if scene_format.is_dummy(s)}
                return bodies, dummies
            if kind in ("caption", "credit"):
                texts = [e.text for e in caption_format.parse(blob).entries]
                d = {i for i, s in enumerate(texts) if scene_format.is_dummy(s)}
                return texts, d
            if kind == "scrdata":
                parsed = msgdata_format.parse(blob)
                ss = [tx for _, tx in msgdata_format.flatten_with_labels(parsed, 1)]
                d = {i for i, s in enumerate(ss) if scene_format.is_dummy(s)}
                return ss, d
        except Exception:
            return None
        return None

    # Build duplicate index so we write ONE bundle entry per content-cluster.
    # All cluster members are written into mods/<id> together on Apply.
    dupe_index = _build_dupe_index(data0_path, data1_path, project_path / "cache")
    # Track which ids we've already emitted (skip duplicates of the same cluster)
    emitted_ids: set[int] = set()
    # String-level dedup: track every unique original string we've already
    # written into the bundle. Subsequent occurrences (in other entries) get
    # skipped via the per-string-skip mechanism; Apply auto-fills them by
    # looking up the translation in the global content_hash map.
    seen_string_hashes: set[str] = set()
    import hashlib as _hashlib

    def _string_hash(s: str) -> str:
        return _hashlib.sha1(s.encode("utf-8")).hexdigest()

    written = 0
    with out_file.open("w", encoding="utf-8", newline="\n") as bundle:
        bundle.write(BUNDLE_HEADER)
        bundle.write(f"# source_lang: {source_lang}\n#\n")

        # Names map for DATA1 entries.
        names = _load_names()

        # Path-based files that supersede their DATA1 counterparts. For these
        # entry IDs we skip DATA1 — patch4/common/common/... is the canonical
        # newer copy that the game actually reads at runtime.
        DATA1_SKIP_IF_PATCHED = {
            0: "msgdata.bin",
            1: "scrdata.bin",
            2: "gwscrdata.bin",
            3: "tuscrdata.bin",
            4: "btlscrdata.bin",
            68: "Credit.bin",
        }

        # 1) DATA1 (silent translatable filter)
        with data1_path.open("rb") as f:
            for e in iter_data0(data0_path):
                if (
                    _TRANSLATABLE_REFERENCE_IDS
                    and e.entry_id not in _TRANSLATABLE_REFERENCE_IDS
                ):
                    continue
                if e.entry_id in DATA1_SKIP_IF_PATCHED:
                    expected = DATA1_SKIP_IF_PATCHED[e.entry_id]
                    if (romfs_path / "patch4" / "common" / "common" / expected).exists() or \
                       (romfs_path / "patch4" / "common" / "common" / "caption" / expected).exists():
                        continue
                try:
                    head = peek_entry_head(f, e, 256)
                except Exception:
                    continue
                kind = _classify_head(head, e.decompressed_size)
                if kind is None:
                    continue
                # Lang gating: filename first, then content sample for TextS.
                entry_name = names.get(e.entry_id, "")
                lang_by_name = (
                    detect_from_filename(entry_name) if entry_name else "UNKNOWN"
                )
                if lang_by_name != "UNKNOWN" and lang_by_name != desired_label:
                    continue
                if kind == "texts" and lang_by_name == "UNKNOWN":
                    try:
                        sample = _decode_texts_sample(f, e, head)
                        lang_label = detect_lang_label(sample)
                    except Exception:
                        lang_label = "UNKNOWN"
                    if lang_label != desired_label:
                        continue
                try:
                    blob = read_entry_full(f, e)
                except Exception:
                    continue
                result = _strings_of(blob, kind)
                if not result:
                    continue
                strings, dummies = result
                if not strings:
                    continue
                # Skip if another id from this cluster was already emitted
                if e.entry_id in emitted_ids:
                    continue
                cluster = dupe_index.get(e.entry_id, [e.entry_id])
                emitted_ids.update(cluster)
                # MERGE: if user already translated this entry (or any of its
                # cluster mates) in a previous bundle, keep their text.
                merged = None
                for cid in [e.entry_id] + cluster:
                    cand = existing_translations.get(f"data1:{cid}")
                    if cand and len(cand) == len(strings):
                        merged = cand
                        break
                emit = merged if merged is not None else strings
                # If the whole entry is dummies, skip it entirely.
                if dummies and len(dummies) == len(strings):
                    continue
                # String-level dedup: hash ORIGINAL strings (not user-translated
                # bodies) so dupes are detected regardless of merge state.
                skip = set(dummies)
                for i, s in enumerate(strings):
                    if i in skip:
                        continue
                    h = _string_hash(s)
                    if h in seen_string_hashes:
                        skip.add(i)        # already shown elsewhere
                    else:
                        seen_string_hashes.add(h)
                if len(skip) == len(strings):
                    continue   # entire entry is dummies/dupes — nothing new
                bundle.write("\n=== ENTRY ===\n")
                bundle.write("source: data1\n")
                bundle.write(f"kind: {kind}\n")
                bundle.write(f"id: {e.entry_id}\n")
                if entry_name:
                    bundle.write(f"name: {entry_name}\n")
                if len(cluster) > 1:
                    other = [x for x in cluster if x != e.entry_id]
                    bundle.write(f"# also-replicates-to: {', '.join(map(str, other))}\n")
                bundle.write(f"strings: {len(emit)}\n")
                bundle.write(_strings_to_block(emit, skip_indices=skip))
                bundle.write("\n")
                written += 1

        # 2) patch4 path-based — every translatable .bin
        patch4 = romfs_path / "patch4"
        if patch4.is_dir():
            for f in sorted(patch4.rglob("*.bin")):
                if not f.is_file():
                    continue
                sz = f.stat().st_size
                if sz < 32 or sz > 50_000_000:
                    continue
                lang_by_name = detect_from_filename(f.name)
                if lang_by_name == "UNKNOWN":
                    parent = f.parent.name
                    lang_by_name = SOURCE_TO_LABEL.get(parent.upper(), "UNKNOWN")
                if lang_by_name != "UNKNOWN" and lang_by_name != desired_label:
                    continue
                try:
                    with f.open("rb") as h:
                        head = h.read(256)
                except Exception:
                    continue
                kind = _classify_head(head, sz)
                if kind is None:
                    continue
                try:
                    blob = f.read_bytes()
                except Exception:
                    continue
                result = _strings_of(blob, kind)
                if not result:
                    continue
                strings, dummies = result
                if not strings:
                    continue
                rel = f.relative_to(romfs_path).as_posix()
                prev = existing_translations.get(f"path:{rel}")
                emit = prev if (prev is not None and len(prev) == len(strings)) else strings
                if dummies and len(dummies) == len(strings):
                    continue
                # String-level dedup: skip indices whose original content was
                # already shown in this bundle. Apply propagates the translation
                # via the content_hash map.
                skip = set(dummies)
                for i, s in enumerate(strings):
                    if i in skip:
                        continue
                    h = _string_hash(s)
                    if h in seen_string_hashes:
                        skip.add(i)
                    else:
                        seen_string_hashes.add(h)
                if len(skip) == len(strings):
                    continue
                bundle.write("\n=== ENTRY ===\n")
                bundle.write("source: path\n")
                bundle.write(f"kind: {kind}\n")
                bundle.write(f"path: {rel}\n")
                bundle.write(f"strings: {len(emit)}\n")
                bundle.write(_strings_to_block(emit, skip_indices=skip))
                bundle.write("\n")
                written += 1

    return {
        "out_dir": str(project_path),
        "bundle_path": str(out_file),
        "total_written": written,
    }


def _build_dupe_index(data0_path: Path, data1_path: Path,
                       cache_dir: Path) -> dict[int, list[int]]:
    """Build (and cache) a map gid → list of duplicate gids that share the
    same translatable string content. Lets Apply auto-write translations into
    every locale variant without the user duplicating them in bundle.txt.

    Cache file: <cache_dir>/data1_dupe_index.json. Rebuilt automatically if
    DATA1.bin mtime changes."""
    import json, hashlib
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "data1_dupe_index.json"
    sig = f"{data1_path.stat().st_size}-{int(data1_path.stat().st_mtime)}"
    if cache_file.exists():
        try:
            obj = json.loads(cache_file.read_text(encoding="utf-8"))
            if obj.get("sig") == sig:
                return {int(k): v for k, v in obj["index"].items()}
        except Exception:
            pass

    # Build
    hash_to_ids: dict[str, list[int]] = {}
    with data1_path.open("rb") as f:
        for e in iter_data0(data0_path):
            if e.decompressed_size < 32 or e.decompressed_size > 500_000:
                continue
            try:
                head = peek_entry_head(f, e, 256)
            except Exception:
                continue
            kind = _classify_head(head, e.decompressed_size)
            if kind not in ("texts", "scene", "caption", "credit"):
                continue
            try:
                blob = read_entry_full(f, e)
            except Exception:
                continue
            try:
                if kind == "texts":
                    strings = texts_format.parse(blob).strings
                elif kind == "scene":
                    strings = scene_format.parse(blob).strings
                else:
                    strings = [c.text for c in caption_format.parse(blob).entries]
            except Exception:
                continue
            if not strings:
                continue
            h = hashlib.sha1("\x1f".join(strings).encode("utf-8")).hexdigest()
            hash_to_ids.setdefault(h, []).append(e.entry_id)

    # Build flat index: each id → list of all dupes (including self)
    index: dict[int, list[int]] = {}
    for ids in hash_to_ids.values():
        if len(ids) > 1:
            ids_sorted = sorted(ids)
            for i in ids_sorted:
                index[i] = ids_sorted

    cache_file.write_text(
        json.dumps({"sig": sig, "index": {str(k): v for k, v in index.items()}}),
        encoding="utf-8",
    )
    return index


def handle_apply_bundle(params):
    """Read a translation_bundle.txt and re-emit each entry into project/romfs/.

    DATA1 entries become indexed mods (<project>/romfs/mods/<id>).
    Path entries become path-based overrides (<project>/romfs/<rel>).
    target_slot picks which language slot to replace for ScrData (default 2 = EN-UK).

    AUTO-DUPLICATE: many DATA1 text entries are content-duplicates across
    locale variants (e.g. entry 9553 = US English, 11356 = UK English have
    identical original strings). Translating one writes to ALL — index built
    from DATA1 content hashes, cached in <project>/cache/data1_dupe_index.json.
    """
    project_path = Path(params["project_path"]).resolve()
    bundle_path = Path(params["bundle_path"])
    romfs_path = Path(params["romfs_path"])
    data0_path = romfs_path / "DATA0.bin"
    data1_path = romfs_path / "DATA1.bin"
    target_slot = int(params.get("target_slot", 2))

    # Build / load duplicate index for DATA1 text entries
    dupe_index = _build_dupe_index(data0_path, data1_path, project_path / "cache")
    # String-level content map: hash(original_string) -> translated_string.
    # Built from the bundle so we can fill in entries that were skipped at
    # extract-time as duplicates (user wrote translation only once).
    import hashlib as _hashlib
    def _hh(s: str) -> str:
        return _hashlib.sha1(s.encode("utf-8")).hexdigest()
    content_translation: dict[str, str] = {}

    # Build entry_id -> Data0Entry index ONCE (instead of O(n) scan per lookup).
    # Without this, 3000+ worker jobs × 40k entries = 100M+ operations.
    data0_index: dict[int, "Data0Entry"] = {e.entry_id: e for e in iter_data0(data0_path)}

    # Cache for parsed path-based scrdata blobs (msgdata.bin etc). Many entries
    # would otherwise reparse the same 9 MB file.
    path_blob_cache: dict[str, bytes] = {}
    path_orig_strings_cache: dict[tuple[str, str], list[str]] = {}  # (path, kind) -> orig strings list

    def _get_path_blob(rel: str) -> bytes:
        if rel not in path_blob_cache:
            path_blob_cache[rel] = (romfs_path / rel).read_bytes()
        return path_blob_cache[rel]

    def _get_path_orig_strings(rel: str, kind: str) -> list[str]:
        key = (rel, kind)
        if key in path_orig_strings_cache:
            return path_orig_strings_cache[key]
        blob = _get_path_blob(rel)
        if kind == "texts":
            ss = texts_format.parse(blob).strings
        elif kind == "scene":
            raw = scene_format.parse(blob).strings
            ss = [scene_format.split_markers(s)[1] for s in raw]
        elif kind in ("caption", "credit"):
            ss = [c.text for c in caption_format.parse(blob).entries]
        elif kind == "scrdata":
            parsed = msgdata_format.parse(blob)
            ss = [t for _, t in msgdata_format.flatten_with_labels(parsed, 1)]
        else:
            ss = []
        path_orig_strings_cache[key] = ss
        return ss

    text = bundle_path.read_text(encoding="utf-8")
    # Split into entry blocks. Skip the file preamble (header comments before
    # the first `=== ENTRY ===` marker) — it's not an entry.
    blocks = []
    cur: list[str] = []
    in_entry = False
    for line in text.splitlines():
        if line.strip() == "=== ENTRY ===":
            if in_entry and cur:
                blocks.append("\n".join(cur))
            cur = []
            in_entry = True
        elif in_entry:
            cur.append(line)
    if in_entry and cur:
        blocks.append("\n".join(cur))

    # Parse all bundle blocks into (meta, kind, strings, source) tuples up
    # front (single-threaded but cheap), then dispatch the heavy work
    # (DATA1 reads + format serialization + mod-file writes) onto a thread
    # pool. Apply is mostly I/O + ctypes-released pure-Python work, so a
    # ThreadPoolExecutor gives a real speedup despite the GIL.
    import re as _re
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Pre-pass: scan ALL entries to build content_translation map (hash of
    # original string -> user's translated string). Without this, entries
    # whose strings were skipped at extract (string-level dupes) would lose
    # the translation. Each non-empty translated string at position i in a
    # block tells us "user translated original[entry,i] as <translation>".
    def _build_content_map():
        with data1_path.open("rb") as df:
            for blk in blocks:
                if not blk.strip():
                    continue
                # Find the FIRST string marker in this block — could be #0, #1, #50,
                # etc., depending on which positions survived dedup/dummy filters.
                m_inner = _re.search(r"^(?:#\d+\b|--- \[\d+\] ---)", blk, _re.MULTILINE)
                if not m_inner:
                    continue
                hdr = blk[:m_inner.start()]
                body = blk[m_inner.start():]
                meta: dict[str, str] = {}
                for line in hdr.splitlines():
                    if ":" in line and not line.lstrip().startswith("#"):
                        k, _, v = line.partition(":")
                        meta[k.strip().lower()] = v.strip()
                try:
                    declared = int(meta.get("strings", "0"))
                except Exception:
                    declared = 0
                # Recover count from #N markers if `strings:` was deleted by translator
                expected = _infer_expected_count(body, declared)
                if expected <= 0:
                    continue
                try:
                    bundle_strings = _block_to_strings(body, expected)
                except Exception:
                    continue
                if meta.get("source") == "data1":
                    try:
                        eid = int(meta["id"])
                        entry = data0_index.get(eid)
                        if entry is None:
                            continue
                        original_blob = read_entry_full(df, entry)
                    except Exception:
                        continue
                    kind = meta.get("kind", "")
                    try:
                        if kind == "texts":
                            orig = texts_format.parse(original_blob).strings
                        elif kind == "scene":
                            raw = scene_format.parse(original_blob).strings
                            orig = [scene_format.split_markers(s)[1] for s in raw]
                        elif kind in ("caption", "credit"):
                            orig = [c.text for c in caption_format.parse(original_blob).entries]
                        elif kind == "scrdata":
                            parsed = msgdata_format.parse(original_blob)
                            orig = [t for _, t in msgdata_format.flatten_with_labels(parsed, 1)]
                        else:
                            continue
                    except Exception:
                        continue
                    for i, tr in enumerate(bundle_strings):
                        if tr and i < len(orig) and tr != orig[i]:
                            h = _hh(orig[i])
                            content_translation.setdefault(h, tr)
                elif meta.get("source") == "path":
                    rel = meta.get("path")
                    if not rel:
                        continue
                    kind = meta.get("kind", "")
                    try:
                        orig = _get_path_orig_strings(rel, kind)
                    except Exception:
                        continue
                    if not orig:
                        continue
                    for i, tr in enumerate(bundle_strings):
                        if tr and i < len(orig) and tr != orig[i]:
                            h = _hh(orig[i])
                            content_translation.setdefault(h, tr)
    _build_content_map()

    warnings: list[str] = []
    jobs: list[dict] = []
    ordinal = 0  # 1-based bundle position, for warning labels
    for block in blocks:
        if not block.strip():
            continue
        ordinal += 1
        m = _re.search(r"^(?:#\d+\b|--- \[\d+\] ---)", block, _re.MULTILINE)
        body_match = m.start() if m else -1
        if body_match < 0:
            # No string markers at all — block is just a header / blank.
            # Try to label it so the user can find it.
            tmp_meta: dict[str, str] = {}
            for line in block.splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition(":")
                    tmp_meta[k.strip().lower()] = v.strip()
            label = _entry_label(ordinal, tmp_meta)
            warnings.append(f"{label}: no string markers — block skipped")
            continue
        header = block[:body_match]
        body = block[body_match:]
        meta: dict[str, str] = {}
        for line in header.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
        label = _entry_label(ordinal, meta)
        # Validate required fields
        if not meta.get("kind"):
            warnings.append(f"{label}: missing 'kind' field — block skipped")
            continue
        if meta.get("source") == "data1" and not meta.get("id"):
            warnings.append(f"{label}: missing 'id' field for source=data1 — block skipped")
            continue
        if meta.get("source") == "path" and not meta.get("path"):
            warnings.append(f"{label}: missing 'path' field for source=path — block skipped")
            continue
        try:
            kind = meta["kind"]
            try:
                declared = int(meta.get("strings", "0"))
            except Exception:
                declared = 0
            expected = _infer_expected_count(body, declared)
            if declared <= 0 and expected > 0:
                warnings.append(
                    f"{label}: missing 'strings: N' header — recovered count={expected} from #N markers"
                )
            else:
                max_marker = _scan_max_marker(body)
                if max_marker >= declared > 0:
                    # Extra markers beyond declared count = garbage; ignored at apply.
                    warnings.append(
                        f"{label}: declared strings={declared} but found marker #{max_marker} — "
                        f"extra markers IGNORED (declared count is authoritative)"
                    )
            strings = _block_to_strings(body, expected)
            source = meta.get("source")
            jobs.append({
                "kind": kind,
                "strings": strings,
                "source": source,
                "meta": meta,
            })
        except Exception as ex:
            jobs.append({"error": f"parse: {ex}", "meta": meta})

    written = 0
    skipped_unchanged = 0
    errors: list[str] = []
    write_lock_obj = None  # mkdir + write are atomic per-file; no shared lock needed

    def _fill_skipped_from_content_map(kind: str, strings: list[str],
                                        original_blob: bytes,
                                        orig: list[str] | None = None) -> list[str]:
        """For each empty/missing string in `strings`, look up the original
        body's content hash in the global content_translation map and use
        the translation found elsewhere in the bundle. Falls back to original
        if no translation exists. `orig` can be pre-computed (e.g. cached
        for path-based scrdata) to avoid reparsing big blobs."""
        if not any(s == "" for s in strings):
            return strings
        if orig is None:
            try:
                if kind == "texts":
                    orig = texts_format.parse(original_blob).strings
                elif kind == "scene":
                    raw = scene_format.parse(original_blob).strings
                    orig = [scene_format.split_markers(s)[1] for s in raw]
                elif kind in ("caption", "credit"):
                    orig = [c.text for c in caption_format.parse(original_blob).entries]
                elif kind == "scrdata":
                    parsed = msgdata_format.parse(original_blob)
                    orig = [t for _, t in msgdata_format.flatten_with_labels(parsed, 1)]
                else:
                    return strings
            except Exception:
                return strings
        out = list(strings)
        for i in range(min(len(out), len(orig))):
            if out[i] == "":
                h = _hh(orig[i])
                if h in content_translation:
                    out[i] = content_translation[h]
                else:
                    # No translation anywhere — keep the original so the game
                    # gets a meaningful (English) fallback instead of "".
                    out[i] = orig[i]
        return out

    def _do_data1(kind, entry_id, strings, all_ids):
        """Worker: replicate translation into every duplicate gid."""
        local_written = 0
        local_skipped = 0
        local_errors: list[str] = []
        # One DATA1 handle per worker (thread-safe seek/read)
        with data1_path.open("rb") as f:
            for dup_id in all_ids:
                try:
                    entry = data0_index.get(dup_id)
                    if entry is None:
                        local_errors.append(f"data1/{dup_id}: not in DATA0 index")
                        continue
                    original_blob = read_entry_full(f, entry)
                except Exception as ex:
                    local_errors.append(f"data1/{dup_id}: read: {ex}")
                    continue
                try:
                    # Fill skipped (dup-of-other-entry) positions from global content map
                    filled = _fill_skipped_from_content_map(kind, strings, original_blob)
                    if _strings_unchanged(kind, original_blob, filled):
                        if dup_id == entry_id:
                            local_skipped += 1
                        continue
                    new_blob = _reinsert_with_slot(
                        kind, original_blob, filled, target_slot, project_path
                    )
                    out_path = project_path / "romfs" / "mods" / str(dup_id)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(new_blob)
                    local_written += 1
                except Exception as ex:
                    local_errors.append(f"data1/{dup_id}: write: {ex}")
        return local_written, local_skipped, local_errors

    def _do_path(kind, rel, strings):
        try:
            original_blob = _get_path_blob(rel)
            # Use cached parsed strings (avoids re-parsing 9 MB msgdata for each call)
            try:
                orig_strings = _get_path_orig_strings(rel, kind)
            except Exception:
                orig_strings = None
            filled = _fill_skipped_from_content_map(kind, strings, original_blob, orig=orig_strings)
            if _strings_unchanged(kind, original_blob, filled):
                return 0, 1, []
            new_blob = _reinsert_with_slot(
                kind, original_blob, filled, target_slot, project_path
            )
            out_path = project_path / "romfs" / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(new_blob)
            return 1, 0, []
        except Exception as ex:
            return 0, 0, [f"path/{rel}: {ex}"]

    def _job_runner(j):
        if "error" in j:
            return 0, 0, [j["error"]]
        meta = j["meta"]
        source = j["source"]
        if source == "data1":
            try:
                entry_id = int(meta["id"])
            except Exception:
                return 0, 0, [f"bad id: {meta.get('id')!r}"]
            all_ids = dupe_index.get(entry_id, [entry_id])
            return _do_data1(j["kind"], entry_id, j["strings"], all_ids)
        if source == "path":
            rel = meta.get("path")
            if not rel:
                return 0, 0, ["path source missing 'path' meta"]
            return _do_path(j["kind"], rel, j["strings"])
        return 0, 0, [f"unknown source: {source}"]

    # Tune worker count to local cores; cap at 8 to keep DATA1 disk seeking sane
    import os as _os
    max_workers = min(8, max(2, (_os.cpu_count() or 4) - 1))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_job_runner, j) for j in jobs]
        for fut in as_completed(futures):
            try:
                w, s, errs = fut.result()
            except Exception as ex:
                errors.append(f"worker crash: {ex}")
                continue
            written += w
            skipped_unchanged += s
            errors.extend(errs)

    return {
        "applied": written,
        "skipped_unchanged": skipped_unchanged,
        "errors_count": len(errors),
        "errors_sample": errors[:10],
        "warnings_count": len(warnings),
        "warnings": warnings[:50],
        "workers": max_workers,
    }


def _strings_unchanged(kind: str, original_blob: bytes, new_strings: list[str], target_slot: int = 1) -> bool:
    """True iff `new_strings` matches strings already in the original blob's
    SOURCE slot (slot 1 = ENG_U for ScrData). target_slot kept for future."""
    try:
        if kind == "texts":
            cur = texts_format.parse(original_blob).strings
        elif kind == "scene":
            # Compare body-only (bundle has stripped markers)
            cur = [scene_format.split_markers(s)[1]
                   for s in scene_format.parse(original_blob).strings]
        elif kind in ("caption", "credit"):
            cur = [e.text for e in caption_format.parse(original_blob).entries]
        elif kind == "scrdata":
            parsed = msgdata_format.parse(original_blob)
            cur = [t for _, t in msgdata_format.flatten_with_labels(parsed, 1)]
        else:
            return False
    except Exception:
        return False
    return cur == list(new_strings)


def _reinsert_with_slot(kind: str, original_blob: bytes, strings: list[str], target_slot: int, project_path: Path | None = None) -> bytes:
    strings = _apply_lookalike_substitutions(strings, project_path)
    if kind == "scrdata":
        original = msgdata_format.parse(original_blob)
        return msgdata_format.replace_language(original, target_slot, strings)
    return _reinsert(kind, original_blob, strings, project_path)


# Substitute UA-only codepoints with closest glyphs that exist in the
# native font00_JPN. Used as a fallback ONLY when no font patch is active —
# when the project carries a patched UTF8TBL_JPN, we let real Є/є/Ґ/ґ flow
# through so the emulator's font mod (if it loads) can render real glyphs.
UA_LOOKALIKE_MAP_FULL = {
    "Ї": "Ï", "ї": "ï",
    "І": "I", "і": "i",
    "Є": "Е", "є": "е",
    "Ґ": "Г", "ґ": "г",
}
UA_LOOKALIKE_MAP_FONT_PATCHED = {
    # When the font is patched (mods/77 exists), UTF8TBL maps Є→gid 285
    # (mirrored Э cell) and є→gid 317 (mirrored э cell) — so Є/є pass
    # through unchanged and render as real Ukrainian glyphs in-game.
    # Ї/ї/І/і/Ґ/ґ still need text-level substitute (no atlas paint yet).
    "Ї": "Ï", "ї": "ï",
    "І": "I", "і": "i",
    "Ґ": "Г", "ґ": "г",
}


def _project_font_patched(project_path: Path | None) -> bool:
    """True iff the font editor has produced an indexed mod for UTF8TBL (entry 77)."""
    if not project_path:
        return False
    return (project_path / "romfs" / "mods" / "77").exists()


def _apply_lookalike_substitutions(strings: list[str], project_path: Path | None = None) -> list[str]:
    # When mods/77 is present, the patched UTF8TBL maps Є/є codepoints to
    # the gids of the (now horizontally-mirrored) Э/э cells in the atlas
    # — so we let Є/є flow through untouched. Otherwise fall back to a
    # FULL look-alike substitute so the translation stays readable.
    mapping = (
        UA_LOOKALIKE_MAP_FONT_PATCHED
        if _project_font_patched(project_path)
        else UA_LOOKALIKE_MAP_FULL
    )
    table = str.maketrans(mapping)
    return [s.translate(table) for s in strings]


def _reinsert(kind: str, original_blob: bytes, strings: list[str], project_path: Path | None = None) -> bytes:
    strings = _apply_lookalike_substitutions(strings, project_path)
    if kind == "texts":
        parsed = texts_format.parse(original_blob)
        return texts_format.serialize(
            strings,
            reserved_raw=parsed.header.reserved_raw,
            pad_after_ptrs=parsed.header.pad_after_ptrs,
        )
    if kind == "scene":
        original = scene_format.parse(original_blob)
        # If a string in the bundle was empty/missing (dummy entry skipped at
        # extract time) — fall back to the original body for that index.
        bodies: list[str] = []
        for i, s in enumerate(strings):
            if s == "" and i < len(original.strings):
                _, body, _ = scene_format.split_markers(original.strings[i])
                bodies.append(body)
            else:
                bodies.append(s)
        with_markers = scene_format.reapply_markers_from_original(bodies, original.strings)
        return scene_format.serialize(with_markers, original=original)
    if kind in ("caption", "credit"):
        original = caption_format.parse(original_blob)
        # Fill empty positions (skipped dummies / control commands) from original
        orig_texts = [e.text for e in original.entries]
        filled = list(strings)
        for i in range(min(len(filled), len(orig_texts))):
            if not filled[i]:
                filled[i] = orig_texts[i]
        return caption_format.serialize(original, filled)
    if kind == "scrdata":
        original = msgdata_format.parse(original_blob)
        return msgdata_format.replace_language(original, 2, strings)
    raise ValueError(f"_reinsert: unsupported kind '{kind}'")


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


def handle_translation_progress(params):
    """Walk bundle.txt + DATA1/path originals to count how many strings have
    been translated (text differs from English original) vs left untouched."""
    import re as _re
    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()
    bundle_path = project_path / "translation_bundle.txt"
    if not bundle_path.exists():
        raise RuntimeError(f"bundle not found at {bundle_path}")

    data0_path = romfs_path / "DATA0.bin"
    data1_path = romfs_path / "DATA1.bin"

    text = bundle_path.read_text(encoding="utf-8")
    blocks = _re.split(r"(?m)^=== ENTRY ===\n", text)[1:]

    total = 0
    translated = 0
    chars_total = 0
    chars_translated = 0
    by_kind_total: dict[str, int] = {}
    by_kind_done: dict[str, int] = {}

    with data1_path.open("rb") as df:
        for blk in blocks:
            m = _re.search(r"^(?:#\d+\b|--- \[\d+\] ---)", blk, _re.MULTILINE)
            if not m:
                continue
            hdr = blk[:m.start()]
            body = blk[m.start():]
            meta: dict[str, str] = {}
            for line in hdr.splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip()
            try:
                expected = int(meta.get("strings", "0"))
            except Exception:
                continue
            if expected <= 0:
                continue
            try:
                bundle_strings = _block_to_strings(body, expected)
            except Exception:
                continue

            kind = meta.get("kind", "")
            source = meta.get("source", "")
            try:
                if source == "data1":
                    eid = int(meta["id"])
                    entry = _find_data1_entry(data0_path, eid)
                    orig_blob = read_entry_full(df, entry)
                elif source == "path":
                    orig_blob = (romfs_path / meta["path"]).read_bytes()
                else:
                    continue
                if kind == "texts":
                    orig = texts_format.parse(orig_blob).strings
                elif kind == "scene":
                    raw = scene_format.parse(orig_blob).strings
                    orig = [scene_format.split_markers(s)[1] for s in raw]
                elif kind in ("caption", "credit"):
                    orig = [c.text for c in caption_format.parse(orig_blob).entries]
                elif kind == "scrdata":
                    parsed = msgdata_format.parse(orig_blob)
                    orig = [t for _, t in msgdata_format.flatten_with_labels(parsed, 1)]
                else:
                    continue
            except Exception:
                continue

            for i, tr in enumerate(bundle_strings):
                if not tr:
                    continue  # gap (dedup / dummy)
                if i >= len(orig):
                    continue
                total += 1
                chars_total += len(orig[i])
                by_kind_total[kind] = by_kind_total.get(kind, 0) + 1
                # Compare ignoring trailing whitespace: extract normalizes
                # trailing spaces/newlines, so untouched strings would other-
                # wise count as "translated" (1717 false positives measured).
                if tr.rstrip() != orig[i].rstrip():
                    translated += 1
                    chars_translated += len(orig[i])
                    by_kind_done[kind] = by_kind_done.get(kind, 0) + 1

    pct = (100 * translated / total) if total else 0.0
    pct_chars = (100 * chars_translated / chars_total) if chars_total else 0.0
    by_kind = []
    for k in sorted(by_kind_total.keys()):
        d = by_kind_done.get(k, 0)
        t = by_kind_total[k]
        by_kind.append({"kind": k, "done": d, "total": t,
                        "pct": (100 * d / t) if t else 0.0})

    return {
        "total_strings": total,
        "translated": translated,
        "untranslated": total - translated,
        "percent": round(pct, 2),
        "chars_total": chars_total,
        "chars_translated": chars_translated,
        "chars_percent": round(pct_chars, 2),
        "by_kind": by_kind,
    }


def handle_pack_for_switch(params):
    import shutil
    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()
    title_id = params.get("title_id") or get_config().get("title_id", "010055D009F78000")
    orig_info0 = romfs_path / "patch4" / "INFO0.bin"
    orig_info2 = romfs_path / "patch4" / "INFO2.bin"
    if not orig_info0.exists() or not orig_info2.exists():
        raise ValueError("original patch4/INFO0.bin or INFO2.bin not found")
    project_romfs = project_path / "romfs"
    project_mods = project_romfs / "mods"
    build_root = project_path / "build" / "atmosphere" / "contents" / title_id / "romfs"
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True, exist_ok=True)

    # Font patcher already writes mods/72 + mods/77 directly into project_mods,
    # so we don't need to mirror anything further; build_info0_overlay will
    # pick them up by scanning the mods/ directory.

    new_info0, new_info2, added = info_patch.build_info0_overlay(
        original_info0_path=orig_info0,
        original_info2_path=orig_info2,
        mods_dir=project_mods,
    )
    patch4_out = build_root / "patch4"
    patch4_out.mkdir(parents=True, exist_ok=True)
    (patch4_out / "INFO0.bin").write_bytes(new_info0)
    (patch4_out / "INFO2.bin").write_bytes(new_info2)

    mod_count = 0
    if project_mods.is_dir():
        out_mods = build_root / "mods"
        out_mods.mkdir(parents=True, exist_ok=True)
        for f in project_mods.iterdir():
            if f.is_file() and f.name.isdigit():
                shutil.copy2(f, out_mods / f.name)
                mod_count += 1

    path_files = 0
    if project_romfs.is_dir():
        for src in project_romfs.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(project_romfs).as_posix()
            if rel.startswith("mods/"):
                continue
            dst = build_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            path_files += 1

    # Optional: also mirror the generated romfs into the Eden emulator's
    # per-title load folder (overwriting whatever's there) and launch Eden.
    deployed_to = None
    launched = False
    if params.get("deploy_to_eden"):
        import os, time, subprocess
        # Kill any lingering Eden process that might be holding files open.
        # WinError 32 ("being used by another process") here is almost always
        # Eden mmap'ing mods/* files; a fresh process forces a clean handoff.
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "eden.exe"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        time.sleep(0.5)
        eden_load = Path(os.path.expandvars(r"%APPDATA%\eden\load")) / title_id
        eden_root = eden_load / "UA" / "romfs"
        # Probe the actual target file (mods/72 is the one Eden mmap-locks).
        try:
            eden_root.mkdir(parents=True, exist_ok=True)
            probe_target = eden_root / "mods" / "72"
            probe_target.parent.mkdir(parents=True, exist_ok=True)
            if probe_target.exists():
                probe_target.unlink()    # raises if locked
            probe_target.write_bytes(b"")
            probe_target.unlink()
        except OSError as e:
            raise RuntimeError(
                f"Eden mod folder {eden_root} is locked (WinError 32). "
                f"This is a known Windows kernel-handle leak from Eden. "
                f"Fix: close Eden completely (Task Manager → eden.exe → End task), "
                f"then REBOOT Windows. After reboot, deploy works cleanly."
            ) from e
        # Retry rmtree a few times — Windows may still hold a handle briefly
        # after a process exits.
        for attempt in range(5):
            if not eden_root.exists():
                break
            try:
                shutil.rmtree(eden_root)
                break
            except OSError:
                time.sleep(0.5)
        eden_root.mkdir(parents=True, exist_ok=True)
        for src in build_root.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(build_root).as_posix()
            dst = eden_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Retry copy if target file is locked by stale handle.
            last_err = None
            for attempt in range(5):
                try:
                    if dst.exists():
                        dst.unlink()
                    shutil.copy2(src, dst)
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    time.sleep(0.3)
            if last_err is not None:
                raise last_err
        deployed_to = str(eden_root)

        # Force OS to flush our writes to disk so Eden sees the new files
        # rather than stale/empty pages when it opens them.
        import time
        for f in eden_root.rglob("*"):
            if f.is_file():
                try:
                    fd = os.open(str(f), os.O_RDONLY)
                    os.fsync(fd)
                    os.close(fd)
                except Exception:
                    pass
        time.sleep(2.0)  # safety margin before Eden starts reading

        # Launch the emulator with the game directly (paths from config).
        import subprocess
        cfg = get_config()
        eden_exe = Path(cfg.get("eden_exe", ""))
        game_nsp = Path(cfg.get("game_image", ""))
        if cfg.get("eden_exe") and eden_exe.exists() and game_nsp.exists():
            try:
                subprocess.Popen(
                    [str(eden_exe), "-f", "-g", str(game_nsp)],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
                launched = True
            except Exception:
                pass

    # Optional: deploy to Ryubing/Ryujinx. Ryujinx supports TWO mod surfaces:
    #   1. native mods: <RyuData>/mods/contents/<TID>/<modname>/romfs/...
    #      (Ryujinx's own LayeredFS implementation, definitely read)
    #   2. sdcard atmosphere stub: <RyuData>/sdcard/atmosphere/contents/<TID>/romfs/...
    #      (full Atmosphere CFW convention; may or may not be picked up
    #       depending on Ryujinx fork & sdcard-emulation settings)
    # We mirror our build_root into BOTH so whichever surface is honoured.
    deployed_to_ryujinx = None
    launched_ryujinx = False
    if params.get("deploy_to_ryujinx"):
        import os
        ryu_candidates = [
            Path(os.path.expandvars(r"%APPDATA%\Ryubing")),
            Path(os.path.expandvars(r"%APPDATA%\Ryujinx")),
        ]
        ryu_data = next((p for p in ryu_candidates if p.exists()), ryu_candidates[0])

        targets = [
            ryu_data / "mods" / "contents" / title_id.lower() / "UA" / "romfs",
            ryu_data / "sdcard" / "atmosphere" / "contents" / title_id.lower() / "romfs",
        ]
        for ryu_root in targets:
            if ryu_root.exists():
                shutil.rmtree(ryu_root, ignore_errors=True)
            ryu_root.mkdir(parents=True, exist_ok=True)
            for src in build_root.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(build_root).as_posix()
                dst = ryu_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        deployed_to_ryujinx = str(targets[0].parent.parent)  # mods/contents/<TID>/

        import time
        for ryu_root in targets:
            for f in ryu_root.rglob("*"):
                if f.is_file():
                    try:
                        fd = os.open(str(f), os.O_RDONLY)
                        os.fsync(fd)
                        os.close(fd)
                    except Exception:
                        pass
        time.sleep(1.0)

        import subprocess
        cfg = get_config()
        ryu_exe_candidates = [
            Path(cfg["ryujinx_exe"]) if cfg.get("ryujinx_exe") else None,
            Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ryubing\Ryujinx.exe")),
            ryu_data / "Ryujinx.exe",
        ]
        ryu_exe = next((p for p in ryu_exe_candidates if p and p.exists()), None)
        game_nsp = Path(cfg.get("game_image", ""))
        if ryu_exe and cfg.get("game_image") and game_nsp.exists():
            try:
                subprocess.Popen(
                    [str(ryu_exe), str(game_nsp)],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
                launched_ryujinx = True
            except Exception:
                pass

    return {
        "build_root": str(build_root),
        "title_id": title_id,
        "indexed_total_in_info0": len(new_info0) // 0x120,
        "indexed_added_to_original": added,
        "indexed_mods_copied": mod_count,
        "path_based_files_copied": path_files,
        "deployed_to_eden": deployed_to,
        "launched_eden": launched,
        "deployed_to_ryujinx": deployed_to_ryujinx,
        "launched_ryujinx": launched_ryujinx,
        "deploy_hint": (
            f"Copy contents of '{(project_path / 'build')}' to root of your SD card "
            f"(merge into existing atmosphere/ folder)."
        ),
    }


def handle_patch_font(params):
    """STEP 1: Export the original font atlas (DATA1 entry 72) as a standard
    DDS that Photoshop / GIMP can open. User edits it (mirror Э→Є cells,
    etc.) and saves over the same file. Then user clicks 'Apply font edit'
    which calls apply_font_edit below."""
    import subprocess
    from formats.data1 import iter_data0, read_entry_full
    from formats.entry72_patch import (G1T_HEADER_SIZE, BC3_PAYLOAD_SIZE,
                                        DDS_HEADER_SIZE)
    import struct

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()

    # Extract entry 72 from DATA1 (raw G1T payload)
    data0 = romfs_path / "DATA0.bin"
    data1 = romfs_path / "DATA1.bin"
    raw_g1t = None
    with data1.open("rb") as f:
        for e in iter_data0(data0):
            if e.entry_id == 72:
                raw_g1t = read_entry_full(f, e)
                break
    if raw_g1t is None:
        raise RuntimeError("DATA1 entry 72 (font atlas) not found")
    if raw_g1t[:4] != b"GT1G":
        raise RuntimeError(f"unexpected magic {raw_g1t[:4]!r}")

    payload = raw_g1t[G1T_HEADER_SIZE:G1T_HEADER_SIZE + BC3_PAYLOAD_SIZE]
    if len(payload) != BC3_PAYLOAD_SIZE:
        raise RuntimeError(
            f"payload size {len(payload):,} != {BC3_PAYLOAD_SIZE:,}"
        )

    # Save raw G1T (so we can splice on apply) + edit-ready DDS
    out_dir = project_path / "font_edit"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_g1t_path = out_dir / "entry72_raw.g1t"
    raw_g1t_path.write_bytes(raw_g1t)

    # Build standard 128-byte DDS_HEADER for 4096×512 BC3 (DXT5, 1 mip)
    DDS_MAGIC = b"DDS "
    DDSD_CAPS = 0x1; DDSD_HEIGHT = 0x2; DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000; DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    width, height = 4096, 512
    linear_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    # DDS_HEADER (124 bytes) layout:
    #   size(4) flags(4) height(4) width(4) pitch(4) depth(4) mipMapCount(4)
    #   reserved1[11] (44) + DDS_PIXELFORMAT(32) + caps[4](16) + reserved2(4)
    header = struct.pack("<4sI", DDS_MAGIC, 124) + \
             struct.pack("<IIIIII", flags, height, width, linear_size, 0, 1) + \
             b"\x00" * (11 * 4) + \
             struct.pack("<II4sIIIII", 32, DDPF_FOURCC, b"DXT5", 0, 0, 0, 0, 0) + \
             struct.pack("<IIIII", DDSCAPS_TEXTURE, 0, 0, 0, 0)
    if len(header) != DDS_HEADER_SIZE:
        raise RuntimeError(f"DDS header size {len(header)} != {DDS_HEADER_SIZE}")

    edit_dds = out_dir / "font_edit.dds"
    edit_dds.write_bytes(header + payload)

    # Open Explorer with the file highlighted (user-friendly hand-off)
    try:
        subprocess.Popen(["explorer", "/select,", str(edit_dds)])
    except Exception:
        pass

    return {
        "edit_dds_path": str(edit_dds),
        "raw_g1t_path": str(raw_g1t_path),
        "next_step": (
            "Edit font_edit.dds in Photoshop NVIDIA plugin (preset: BC3 / "
            "DXT5 / 1 mip / NO premultiplied alpha / NO mipmaps / Color Map), "
            "save over the same file, then click 'Apply font edit' button."
        ),
    }


def handle_apply_font_edit(params):
    """STEP 2: Patch the raw G1T container with the user-edited DDS payload
    and write to project/romfs/mods/72. Also writes UTF8TBL swap to mods/77
    so Є/є codepoints map to gids 285/317 (the mirrored Э/э cells)."""
    from formats.data1 import iter_data0, read_entry_full
    from formats.entry72_patch import patch_entry72_with_dds, safe_write_mod_72
    from formats.fontedit import parse_utf8tbl, serialize_utf8tbl, _find_gid

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()

    edit_dds = project_path / "font_edit" / "font_edit.dds"
    raw_g1t = project_path / "font_edit" / "entry72_raw.g1t"
    if not edit_dds.exists():
        raise RuntimeError(
            f"edited DDS not found at {edit_dds}. Click 'Patch font' first "
            f"to export the original atlas for editing."
        )
    if not raw_g1t.exists():
        raise RuntimeError(f"raw G1T not found at {raw_g1t}; click 'Patch font' first.")

    # Patch raw G1T with edited DDS payload → temp file → install to mods/72
    mods_dir = project_path / "romfs" / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    patched_tmp = project_path / "font_edit" / "entry72_patched.g1t"
    patch_entry72_with_dds(raw_g1t, edit_dds, patched_tmp)
    safe_write_mod_72(patched_tmp, mods_dir / "72", info0_is_compressed=False)

    # UTF8TBL swap: map UA codepoints onto rarely-used Cyrillic glyph gids
    # whose cells the user has mirror-flipped in the atlas. Currently:
    #   U+0404 Є → gid 285 (was Э)
    #   U+0454 є → gid 317 (was э)
    # Future: Ы→Ґ etc. when user paints them.
    raw_tbl = None
    with (romfs_path / "DATA1.bin").open("rb") as f:
        for e in iter_data0(romfs_path / "DATA0.bin"):
            if e.entry_id == 77:
                raw_tbl = read_entry_full(f, e)
                break
    if raw_tbl is None:
        raise RuntimeError("DATA1 entry 77 (UTF8TBL) not found")

    cps = parse_utf8tbl(raw_tbl)
    new_cps = list(cps)
    swaps_applied = {}
    for ua_cp, ru_cp in (
        (0x0404, 0x042D),   # Є → Э slot
        (0x0454, 0x044D),   # є → э slot
        # Add more pairs here as user paints them, e.g.
        # (0x0490, 0x042B), # Ґ → Ы slot
        # (0x0491, 0x044B), # ґ → ы slot
    ):
        gid = _find_gid(cps, ru_cp)
        if gid is not None:
            new_cps[gid] = ua_cp
            swaps_applied[f"U+{ua_cp:04X}"] = gid

    new_tbl = serialize_utf8tbl(new_cps)
    if len(new_tbl) != len(raw_tbl):
        raise RuntimeError(
            f"UTF8TBL size changed: {len(new_tbl)} vs {len(raw_tbl)}"
        )
    (mods_dir / "77").write_bytes(new_tbl)

    return {
        "mods_72_path": str(mods_dir / "72"),
        "mods_72_size": (mods_dir / "72").stat().st_size,
        "mods_77_path": str(mods_dir / "77"),
        "mods_77_size": (mods_dir / "77").stat().st_size,
        "codepoint_to_gid": swaps_applied,
    }


def handle_export_multitex(params):
    """Export multi-texture G1T entry (title screen, abbey map, etc.) as a
    set of standalone DDS files for editing.

    params: {project_path, romfs_path, entry_id}
    Returns: {dds_paths: [...], raw_g1t_path, entry_id}
    """
    from formats.data1 import iter_data0, read_entry_full
    from formats import multi_texture_patch as mtx
    import subprocess

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()
    entry_id = int(params["entry_id"])

    # Extract raw G1T from DATA1
    blob = None
    with (romfs_path / "DATA1.bin").open("rb") as f:
        for e in iter_data0(romfs_path / "DATA0.bin"):
            if e.entry_id == entry_id:
                blob = read_entry_full(f, e)
                break
    if blob is None or blob[:4] != b"GT1G":
        raise RuntimeError(f"DATA1 entry {entry_id} not found or not a G1T")

    out_dir = project_path / "multitex_edit" / str(entry_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_g1t = out_dir / "raw.g1t"
    raw_g1t.write_bytes(blob)

    dds_paths = mtx.export_subtextures(raw_g1t, out_dir)

    # Open folder in Explorer
    try:
        subprocess.Popen(["explorer", str(out_dir)])
    except Exception:
        pass

    return {
        "entry_id": entry_id,
        "raw_g1t_path": str(raw_g1t),
        "dds_paths": [str(p) for p in dds_paths],
        "next_step": (
            f"Edit any subset of tex_*.dds in Photoshop (BC3/DXT5, NO mipmaps, "
            f"NO premultiplied alpha, NO DXT10 header, NO size change). Save over "
            f"the same file. Then click 'Apply multi-texture edit' with entry_id={entry_id}."
        ),
    }


def handle_apply_multitex(params):
    """Patch the raw G1T container with edited DDS payloads → mods/<entry_id>."""
    from formats import multi_texture_patch as mtx
    project_path = Path(params["project_path"]).resolve()
    entry_id = int(params["entry_id"])

    edit_dir = project_path / "multitex_edit" / str(entry_id)
    raw_g1t = edit_dir / "raw.g1t"
    if not raw_g1t.exists():
        raise RuntimeError(
            f"raw G1T not found at {raw_g1t}. Click 'Export multi-texture for editing' first."
        )

    patched_tmp = edit_dir / "patched.g1t"
    mtx.patch_with_edited_ddses(raw_g1t, edit_dir, patched_tmp)

    mods_dir = project_path / "romfs" / "mods"
    out_path = mods_dir / str(entry_id)
    mtx.safe_write_mod(patched_tmp, out_path)

    return {
        "entry_id": entry_id,
        "mods_path": str(out_path),
        "mods_size": out_path.stat().st_size,
    }


def handle_reset_font_patch(params):
    """Remove any installed font mods (mods/72, mods/77) so the game uses
    its built-in atlas. The text-level substitute kicks in as fallback."""
    project_path = Path(params["project_path"]).resolve()
    mods_dir = project_path / "romfs" / "mods"
    removed = []
    for name in ("72", "77", "71", "76"):
        p = mods_dir / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return {"removed": removed}


HANDLERS = {
    "ping": handle_ping,
    "open_project": handle_open_project,
    "patch_font": handle_patch_font,
    "apply_font_edit": handle_apply_font_edit,
    "reset_font_patch": handle_reset_font_patch,
    "export_multitex": handle_export_multitex,
    "apply_multitex": handle_apply_multitex,
    "translation_progress": handle_translation_progress,
    "list_text_categories": handle_list_text_categories,
    "list_texts_in_category": handle_list_texts_in_category,
    "survey_path_files": handle_survey_path_files,
    "scan_data1_texts": handle_scan_data1_texts,
    "read_entry": handle_read_entry,
    "save_entry": handle_save_entry,
    "read_existing_translation_unified": handle_read_existing_translation_unified,
    "extract_all_texts": handle_extract_all_texts,
    "apply_bundle": handle_apply_bundle,
    "pack_for_switch": handle_pack_for_switch,
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"non-JSON input: {line!r} ({e})\n")
            continue
        req_id = msg.get("id", 0)
        method = msg.get("method", "")
        params = msg.get("params")
        handler = HANDLERS.get(method)
        if handler is None:
            _err(req_id, -32601, f"unknown method: {method}")
            continue
        try:
            result = handler(params)
            _ok(req_id, result)
        except Exception as e:
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            _err(req_id, -32000, str(e), data={"traceback": tb})


if __name__ == "__main__":
    main()
