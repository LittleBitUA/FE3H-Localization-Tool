"""Marker handling and dummy-filter regressions for the scene format."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from formats import scene


class SplitMarkersTests(unittest.TestCase):
    def test_full_line(self):
        pre, body, suf = scene.split_markers("[0040]Some text＠035150#0")
        self.assertEqual(pre, "[0040]")
        self.assertEqual(body, "Some text")
        self.assertEqual(suf, "＠035150#0")

    def test_no_markers(self):
        pre, body, suf = scene.split_markers("Plain line")
        self.assertEqual((pre, body, suf), ("", "Plain line", ""))

    def test_voice_only(self):
        pre, body, suf = scene.split_markers("Text＠DummyVoic")
        self.assertEqual(pre, "")
        self.assertEqual(body, "Text")
        self.assertEqual(suf, "＠DummyVoic")

    def test_multiline_body(self):
        raw = "[0002]line one\nline two＠048834#0"
        pre, body, suf = scene.split_markers(raw)
        self.assertEqual(body, "line one\nline two")
        self.assertEqual(scene.merge_markers(pre, body, suf), raw)

    def test_roundtrip_merge(self):
        for raw in [
            "[0040]For thousands of years＠035150#0",
            "[9999]NULL#00＠DummyVoic",
            "no markers at all",
            "＠123456",
        ]:
            self.assertEqual(scene.merge_markers(*scene.split_markers(raw)), raw)


class ReapplyMarkersTests(unittest.TestCase):
    def test_reapply_strips_trailing_newline(self):
        # A trailing newline before ＠NNNNNN breaks the game's parser
        # (infinite-loading) — reapply must strip it.
        original = ["[0002]Hello＠000001#0"]
        translated = ["Привіт\n"]
        out = scene.reapply_markers_from_original(translated, original)
        self.assertEqual(out, ["[0002]Привіт＠000001#0"])

    def test_reapply_keeps_translator_markers(self):
        original = ["[0002]Hello＠000001#0"]
        translated = ["[0002]Привіт＠000001#0"]
        out = scene.reapply_markers_from_original(translated, original)
        self.assertEqual(out, ["[0002]Привіт＠000001#0"])


class IsDummyTests(unittest.TestCase):
    def test_none_is_not_dummy(self):
        # "None" is a legitimate in-game UI string (empty equipment slot).
        self.assertFalse(scene.is_dummy("None"))

    def test_null_body_is_dummy(self):
        self.assertTrue(scene.is_dummy("NULL"))
        self.assertTrue(scene.is_dummy("[9999]NULL#00＠DummyVoic"))

    def test_control_tags_are_dummy(self):
        self.assertTrue(scene.is_dummy("<BGM PLAY>106"))
        self.assertTrue(scene.is_dummy("<SE PLAY>2034"))

    def test_dev_placeholders_are_dummy(self):
        self.assertTrue(scene.is_dummy("tTemporarymessage"))
        self.assertTrue(scene.is_dummy("weapon_untranslated_002"))

    def test_real_text_is_not_dummy(self):
        self.assertFalse(scene.is_dummy("[0002]I am not a placeholder.＠000001#0"))
        self.assertFalse(scene.is_dummy("Iron Sword"))


class SceneSerializeTests(unittest.TestCase):
    def test_four_byte_alignment(self):
        blob = scene.serialize(["a", "bb", "ccc", "dddd"])
        parsed = scene.parse(blob)
        self.assertEqual(parsed.strings, ["a", "bb", "ccc", "dddd"])
        # every offset must be 4-byte aligned
        import struct
        count = struct.unpack("<I", blob[:4])[0]
        for i in range(count):
            off, _ln = struct.unpack("<II", blob[4 + i * 8 : 12 + i * 8])
            self.assertEqual(off % 4, 0, f"entry {i} offset {off} not aligned")

    def test_unchanged_returns_original_bytes(self):
        blob = scene.serialize(["x", "y"])
        parsed = scene.parse(blob)
        self.assertEqual(scene.serialize(parsed.strings, original=parsed), blob)


if __name__ == "__main__":
    unittest.main()
