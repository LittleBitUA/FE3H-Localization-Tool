"""Shared sidecar state and low-level helpers: config-driven caches
(entry names, reference-translation filter, DATA0 index, original-blob
cache) and the byte-level format classifier."""
from __future__ import annotations
import json
import struct
from collections import OrderedDict
from pathlib import Path

from appconfig import get_config
from formats import texts as texts_format          # noqa: F401 (re-export surface)
from formats.data1 import Data0Entry, iter_data0, peek_entry_head, read_entry_full
from formats.lang_detect import detect_lang_label  # noqa: F401


def get_reference_ids() -> set[int]:
    """Current reference-translation id filter (empty set = no filter)."""
    return _TRANSLATABLE_REFERENCE_IDS or set()


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


