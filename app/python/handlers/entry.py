"""Per-entry RPC handlers: read, save, existing-translation lookup."""
from __future__ import annotations
from pathlib import Path

from formats import caption as caption_format
from formats import msgdata as msgdata_format
from formats import scene as scene_format
from formats import texts as texts_format
from core.context import (
    _blob_cache_put,
    _get_original_blob_for_save,
    _load_blob,
)


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


