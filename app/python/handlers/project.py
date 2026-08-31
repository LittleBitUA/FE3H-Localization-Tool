"""Project-level RPC handlers: open dump, scans, classification surveys."""
from __future__ import annotations
import json
import os
import platform
import struct
from pathlib import Path

from formats.data1 import iter_data0, peek_entry_head
from formats.lang_detect import detect_lang_label
from core.context import (
    _classify_head,
    _decode_texts_sample,
    _load_names,
    _scan_lang_dir,
    _try_load_translatable_reference,
    get_reference_ids,
)


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
    ref_ids = get_reference_ids()
    if ref_ids:
        rows = [r for r in rows if r["entry_id"] in ref_ids]

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


