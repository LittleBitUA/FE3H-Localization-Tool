"""Bundle block parser regressions (server._block_to_strings and friends)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server


class BlockToStringsTests(unittest.TestCase):
    def test_simple(self):
        block = "#0\nHello\n\n#1\nWorld\n"
        self.assertEqual(server._block_to_strings(block, 2), ["Hello", "World"])

    def test_gap_stays_empty(self):
        # #1 skipped at extract (dedup/dummy) — must stay "" so apply can
        # auto-fill it from the content map.
        block = "#0\nA\n\n#2\nC\n"
        self.assertEqual(server._block_to_strings(block, 3), ["A", "", "C"])

    def test_multiline_string(self):
        block = "#0\nline one\nline two\n\n#1\nnext\n"
        self.assertEqual(
            server._block_to_strings(block, 2), ["line one\nline two", "next"]
        )

    def test_genuine_trailing_newline_kept(self):
        # A string that legitimately ends with a newline: writer emits
        # body + "\n" (its newline) + "\n" (separator) + "" (empty join line).
        # Only the separator must be stripped.
        strings = ["ends with newline\n", "next"]
        block = server._strings_to_block(strings)
        parsed = server._block_to_strings(block, 2)
        self.assertEqual(parsed, strings)

    def test_legacy_marker_format(self):
        block = "--- [0] ---\nOld style\n"
        self.assertEqual(server._block_to_strings(block, 1), ["Old style"])

    def test_out_of_range_marker_ignored(self):
        block = "#0\nA\n\n#7\ngarbage\n"
        self.assertEqual(server._block_to_strings(block, 1), ["A"])


class InferExpectedCountTests(unittest.TestCase):
    def test_declared_wins(self):
        self.assertEqual(server._infer_expected_count("#0\nA\n#5\nB\n", 3), 3)

    def test_recovers_from_markers_when_missing(self):
        self.assertEqual(server._infer_expected_count("#0\nA\n\n#4\nB\n", 0), 5)


class RoundTripThroughBlockTests(unittest.TestCase):
    def test_write_then_parse_identity(self):
        strings = [
            "plain",
            "multi\nline",
            "with trailing space ",
            "",
            "ukrainian: Ґалатея, Єретик, Їжак",
        ]
        block = server._strings_to_block(strings)
        self.assertEqual(server._block_to_strings(block, len(strings)), strings)


if __name__ == "__main__":
    unittest.main()
