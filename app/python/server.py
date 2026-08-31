"""Python sidecar entry point: JSON-RPC over stdin/stdout (one JSON object
per line). Transport + handler registry only — the actual logic lives in:

    core/context.py     shared caches, format classifier, blob loaders
    core/bundletext.py  translation_bundle.txt block format
    handlers/project.py open dump, scans, surveys
    handlers/entry.py   read / save / existing-translation lookup
    handlers/bundle.py  extract, apply, translation progress
    handlers/deploy.py  LayeredFS build + emulator deploy
    handlers/font.py    font atlas + multi-texture G1T patching
"""
from __future__ import annotations
import json
import os
import sys
import traceback

# Make imports robust regardless of how the interpreter was started
# (system python adds the script dir automatically; the embeddable
# runtime shipped in packaged builds does not).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8")

from handlers.project import (
    handle_ping,
    handle_open_project,
    handle_list_text_categories,
    handle_list_texts_in_category,
    handle_survey_path_files,
    handle_scan_data1_texts,
)
from handlers.entry import (
    handle_read_entry,
    handle_save_entry,
    handle_read_existing_translation_unified,
)
from handlers.bundle import (
    handle_extract_all_texts,
    handle_apply_bundle,
    handle_translation_progress,
)
from handlers.deploy import handle_pack_for_switch
from handlers.font import (
    handle_patch_font,
    handle_apply_font_edit,
    handle_reset_font_patch,
    handle_export_multitex,
    handle_apply_multitex,
)

# ---- back-compat re-exports (tests, tools/bundle_to_weblate.py) ----
from core.context import (          # noqa: F401
    _BLOB_CACHE,
    _classify_head,
    _decode_texts_sample,
    _find_data1_entry,
    _get_data0_index,
    _is_texts_head,
    _load_blob,
    _load_names,
    _try_load_translatable_reference,
    get_reference_ids,
)
from core.bundletext import (       # noqa: F401
    BUNDLE_HEADER,
    _block_to_strings,
    _infer_expected_count,
    _parse_txt,
    _scan_max_marker,
    _serialize_txt,
    _strings_to_block,
)
from formats.data1 import read_entry_full   # noqa: F401
from formats import texts as texts_format   # noqa: F401
from formats import scene as scene_format   # noqa: F401
from formats import caption as caption_format   # noqa: F401
from formats import msgdata as msgdata_format   # noqa: F401


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
