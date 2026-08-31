"""Bundle pipeline RPC handlers: extract, apply, translation progress."""
from __future__ import annotations
from pathlib import Path

from formats import caption as caption_format
from formats import msgdata as msgdata_format
from formats import scene as scene_format
from formats import texts as texts_format
from formats.data1 import Data0Entry, iter_data0, peek_entry_head, read_entry_full
from formats.lang_detect import detect_lang_label
from core.context import (
    _classify_head,
    _decode_texts_sample,
    _find_data1_entry,
    _load_names,
    _try_load_translatable_reference,
    get_reference_ids,
)
from core.bundletext import (
    BUNDLE_HEADER,
    _block_to_strings,
    _entry_label,
    _infer_expected_count,
    _scan_max_marker,
    _strings_to_block,
)


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
                ref_ids = get_reference_ids()
                if ref_ids and e.entry_id not in ref_ids:
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


