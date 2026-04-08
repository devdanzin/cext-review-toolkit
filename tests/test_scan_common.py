"""Tests for scan_common.py shared utilities."""

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "plugins"
        / "cext-review-toolkit"
        / "scripts"
    ),
)

from scan_common import (
    deduplicate_findings,
    has_safety_annotation,
    parse_common_args,
)


class TestDeduplicateFindings(unittest.TestCase):
    """Tests for deduplicate_findings()."""

    def test_empty_list(self):
        self.assertEqual(deduplicate_findings([]), [])

    def test_single_finding(self):
        f = {"type": "leak", "file": "a.c", "detail": "leaked ref at line 10"}
        result = deduplicate_findings([f])
        self.assertEqual(len(result), 1)
        self.assertNotIn("duplicate_count", result[0])

    def test_two_duplicates_grouped(self):
        f1 = {
            "type": "leak",
            "file": "a.c",
            "line": 10,
            "detail": "leaked 'x' at line 10",
        }
        f2 = {
            "type": "leak",
            "file": "a.c",
            "line": 20,
            "detail": "leaked 'y' at line 20",
        }
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["duplicate_count"], 1)
        self.assertEqual(len(result[0]["duplicate_locations"]), 1)

    def test_different_types_not_grouped(self):
        f1 = {"type": "leak", "file": "a.c", "detail": "leaked at line 10"}
        f2 = {"type": "null_deref", "file": "a.c", "detail": "leaked at line 10"}
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_different_files_not_grouped(self):
        f1 = {"type": "leak", "file": "a.c", "detail": "leaked at line 10"}
        f2 = {"type": "leak", "file": "b.c", "detail": "leaked at line 10"}
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_normalization_strips_line_numbers(self):
        f1 = {"type": "leak", "file": "a.c", "detail": "leaked at line 10"}
        f2 = {"type": "leak", "file": "a.c", "detail": "leaked at line 99"}
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 1)

    def test_normalization_strips_variable_names(self):
        f1 = {"type": "leak", "file": "a.c", "detail": "leaked 'foo'"}
        f2 = {"type": "leak", "file": "a.c", "detail": "leaked 'bar'"}
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 1)


class TestHasSafetyAnnotation(unittest.TestCase):
    """Tests for has_safety_annotation()."""

    def test_cext_safe_annotation(self):
        self.assertTrue(has_safety_annotation(["/* cext-safe: intentional leak */"]))

    def test_nolint_annotation(self):
        self.assertTrue(has_safety_annotation(["// NOLINT"]))

    def test_by_design_annotation(self):
        self.assertTrue(has_safety_annotation(["/* by design: we skip the check */"]))

    def test_no_annotation(self):
        self.assertFalse(has_safety_annotation(["/* allocate buffer */"]))

    def test_empty_comments(self):
        self.assertFalse(has_safety_annotation([]))

    def test_deliberately_keyword(self):
        self.assertTrue(has_safety_annotation(["// deliberately not freed"]))


class TestParseCommonArgs(unittest.TestCase):
    """Tests for parse_common_args()."""

    def test_no_args(self):
        target, max_files = parse_common_args([])
        self.assertEqual(target, ".")
        self.assertEqual(max_files, 0)

    def test_path_only(self):
        target, max_files = parse_common_args(["/some/path"])
        self.assertEqual(target, "/some/path")
        self.assertEqual(max_files, 0)

    def test_max_files(self):
        target, max_files = parse_common_args(["path", "--max-files", "10"])
        self.assertEqual(target, "path")
        self.assertEqual(max_files, 10)

    def test_invalid_max_files(self):
        with self.assertRaises(SystemExit):
            parse_common_args(["--max-files", "abc"])


if __name__ == "__main__":
    unittest.main()
