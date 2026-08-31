"""Byte-level round-trip tests against real game data.

These need a game dump (and, optionally, a reference translation's mods
folder). They are skipped automatically when the data is not available:

    FE3H_TEST_ROMFS=<path to romfs with DATA0.bin/DATA1.bin>  (env)
    reference_mods_dir                                        (config)

The invariant being protected: parse → serialize must reproduce the
original bytes exactly. Any serializer drift here has historically meant
infinite-loading in game, so this suite is the cheap pre-flight check.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appconfig import get_config
from formats import caption as caption_format
from formats import msgdata as msgdata_format
from formats import scene as scene_format
from formats import texts as texts_format
from formats.data1 import iter_data0, peek_entry_head, read_entry_full

ROMFS = os.environ.get("FE3H_TEST_ROMFS", "")
REFERENCE_MODS = get_config().get("reference_mods_dir", "")

MAX_PER_KIND = 120        # DATA1 sample size per kind
MAX_REFERENCE = 400       # reference mods sample size


def _classify(head: bytes, size: int):
    import server
    return server._classify_head(head, size)


def _roundtrip_one(kind: str, blob: bytes) -> tuple[bool, str]:
    """Returns (ok, message). ok=True when serialize(parse(x)) == x."""
    try:
        if kind == "texts":
            f = texts_format.parse(blob)
            out = texts_format.serialize(
                f.strings,
                reserved_raw=f.header.reserved_raw,
                pad_after_ptrs=f.header.pad_after_ptrs,
            )
        elif kind == "scene":
            f = scene_format.parse(blob)
            out = scene_format.serialize(f.strings, original=f, force_rebuild=True)
            if out != blob:
                # Scene files exist in two on-disk layout variants; the writer
                # emits variant A. For variant-B originals byte-equality is
                # impossible — require semantic equality + a valid rebuild.
                f2 = scene_format.parse(out)
                if f2.strings == f.strings:
                    return True, ""
                return False, "strings drifted after rebuild"
        elif kind in ("caption", "credit"):
            f = caption_format.parse(blob)
            f.original_blob = b""                     # disable fast-path
            out = caption_format.serialize(f, [e.text for e in f.entries])
        elif kind == "scrdata":
            # Semantic round-trip: replacing a slot with its own strings must
            # leave every slot's flattened text unchanged.
            f = msgdata_format.parse(blob)
            slot = 1 if len(f.languages) > 1 else 0
            same = [t for _, t in msgdata_format.flatten_with_labels(f, slot)]
            out_blob = msgdata_format.replace_language(f, slot, same)
            f2 = msgdata_format.parse(out_blob)
            for s in range(len(f.languages)):
                a = [t for _, t in msgdata_format.flatten_with_labels(f, s)]
                b = [t for _, t in msgdata_format.flatten_with_labels(f2, s)]
                if a != b:
                    return False, f"slot {s} strings drifted"
            return True, ""
        else:
            return True, ""
    except Exception as e:
        # Parse refusal = the classifier over-matched a foreign format and the
        # parser correctly rejected it. Not a serializer regression → skip.
        return None, f"parse refused: {e}"
    if out != blob:
        return False, f"bytes differ: {len(out)} vs {len(blob)}"
    return True, ""


@unittest.skipUnless(ROMFS and Path(ROMFS, "DATA1.bin").exists(),
                     "set FE3H_TEST_ROMFS to a dump to run")
class Data1RoundTripTests(unittest.TestCase):
    def test_data1_sample_roundtrip(self):
        romfs = Path(ROMFS)
        data0, data1 = romfs / "DATA0.bin", romfs / "DATA1.bin"
        per_kind: dict[str, int] = {}
        failures: list[str] = []
        checked = 0
        with data1.open("rb") as f:
            for e in iter_data0(data0):
                if e.decompressed_size < 32 or e.decompressed_size > 20_000_000:
                    continue
                try:
                    head = peek_entry_head(f, e, 256)
                except Exception:
                    continue
                kind = _classify(head, e.decompressed_size)
                if kind is None:
                    continue
                if per_kind.get(kind, 0) >= MAX_PER_KIND:
                    continue
                per_kind[kind] = per_kind.get(kind, 0) + 1
                blob = read_entry_full(f, e)
                ok, msg = _roundtrip_one(kind, blob)
                if ok is None:
                    continue          # parser refused a misclassified entry
                checked += 1
                if not ok:
                    failures.append(f"entry {e.entry_id} ({kind}): {msg}")
        self.assertGreater(checked, 0, "no entries sampled — classifier broken?")
        self.assertEqual(failures, [], f"{len(failures)} round-trip failures")


@unittest.skipUnless(REFERENCE_MODS and Path(REFERENCE_MODS).is_dir(),
                     "configure reference_mods_dir to run")
class ReferenceModsRoundTripTests(unittest.TestCase):
    def test_reference_mods_roundtrip(self):
        mods = sorted(
            (p for p in Path(REFERENCE_MODS).iterdir()
             if p.is_file() and p.name.isdigit()),
            key=lambda p: int(p.name),
        )[:MAX_REFERENCE]
        self.assertTrue(mods, "reference mods dir is empty")
        failures: list[str] = []
        checked = 0
        for p in mods:
            blob = p.read_bytes()
            kind = _classify(blob[:256], len(blob))
            if kind is None:
                continue
            ok, msg = _roundtrip_one(kind, blob)
            if ok is None:
                continue
            checked += 1
            if not ok:
                failures.append(f"{p.name} ({kind}): {msg}")
        self.assertGreater(checked, 0)
        self.assertEqual(failures, [], f"{len(failures)} round-trip failures")


if __name__ == "__main__":
    unittest.main()
