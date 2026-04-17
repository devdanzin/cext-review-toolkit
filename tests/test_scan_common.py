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

import tree_sitter
import tree_sitter_c

from scan_common import (
    deduplicate_findings,
    extract_nearby_comments,
    has_safety_annotation,
    is_in_region,
    make_finding,
    parse_common_args,
)


_C_LANG = tree_sitter.Language(tree_sitter_c.language())
_C_PARSER = tree_sitter.Parser(_C_LANG)


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


class TestExtractNearbyComments(unittest.TestCase):
    """Tests for extract_nearby_comments()."""

    def _parse(self, source: str):
        source_bytes = source.encode("utf-8")
        tree = _C_PARSER.parse(source_bytes)
        return source_bytes, tree

    def test_finds_comment_on_same_line(self):
        src = "int x = 1; // safety: intentional\n"
        src_bytes, tree = self._parse(src)
        comments = extract_nearby_comments(src_bytes, tree, line=1, radius=1)
        self.assertTrue(any("safety:" in c for c in comments))

    def test_finds_comment_within_radius(self):
        src = (
            "// by design: no check\n"
            "int f(void) {\n"
            "    return 0;\n"
            "}\n"
        )
        src_bytes, tree = self._parse(src)
        comments = extract_nearby_comments(src_bytes, tree, line=3, radius=5)
        self.assertTrue(any("by design" in c for c in comments))

    def test_ignores_far_comment(self):
        src = (
            "// unrelated comment\n"
            "int a = 1;\n"
            "int b = 2;\n"
            "int c = 3;\n"
            "int d = 4;\n"
            "int e = 5;\n"
            "int f = 6;\n"
            "int g = 7;\n"
            "int h = 8;\n"
            "int i = 9;\n"
        )
        src_bytes, tree = self._parse(src)
        comments = extract_nearby_comments(src_bytes, tree, line=10, radius=2)
        self.assertFalse(any("unrelated" in c for c in comments))

    def test_no_comments_in_source(self):
        src = "int main(void) { return 0; }\n"
        src_bytes, tree = self._parse(src)
        self.assertEqual(extract_nearby_comments(src_bytes, tree, line=1), [])


class TestHasSafetyAnnotationExtended(unittest.TestCase):
    """Cext-specific safety keywords beyond the original minimal test set."""

    def test_gil_held(self):
        self.assertTrue(has_safety_annotation(["/* gil held */"]))
        self.assertTrue(has_safety_annotation(["// gil-held"]))

    def test_already_locked(self):
        self.assertTrue(has_safety_annotation(["// already locked"]))

    def test_refcount_safe(self):
        self.assertTrue(has_safety_annotation(["/* refcount safe here */"]))

    def test_borrowed_ok(self):
        self.assertTrue(has_safety_annotation(["// borrowed ok: container lives"]))

    def test_thread_safe(self):
        self.assertTrue(has_safety_annotation(["// thread-safe access"]))


class TestIsInRegion(unittest.TestCase):
    """Tests for is_in_region()."""

    def test_empty_regions(self):
        self.assertFalse(is_in_region(10, []))

    def test_inside_region(self):
        self.assertTrue(is_in_region(15, [(10, 20)]))

    def test_on_start_boundary(self):
        self.assertTrue(is_in_region(10, [(10, 20)]))

    def test_on_end_boundary_excluded(self):
        self.assertFalse(is_in_region(20, [(10, 20)]))

    def test_outside_region(self):
        self.assertFalse(is_in_region(25, [(10, 20)]))

    def test_multiple_regions(self):
        self.assertTrue(is_in_region(35, [(10, 20), (30, 40)]))
        self.assertFalse(is_in_region(25, [(10, 20), (30, 40)]))


class TestMakeFinding(unittest.TestCase):
    """Tests for make_finding()."""

    def test_minimal_required_fields(self):
        f = make_finding(
            "leak",
            classification="FIX",
            severity="high",
            detail="leaked ref",
        )
        self.assertEqual(f["type"], "leak")
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["detail"], "leaked ref")
        self.assertEqual(f["function"], "")
        self.assertEqual(f["line"], 0)

    def test_with_extras(self):
        f = make_finding(
            "borrowed_ref_across_call",
            function="my_func",
            line=42,
            classification="FIX",
            severity="high",
            confidence="medium",
            detail="borrowed ref used after call",
            api_call="PyList_GetItem",
            variable="item",
        )
        self.assertEqual(f["function"], "my_func")
        self.assertEqual(f["line"], 42)
        self.assertEqual(f["confidence"], "medium")
        self.assertEqual(f["api_call"], "PyList_GetItem")
        self.assertEqual(f["variable"], "item")

    def test_consistent_key_ordering(self):
        f = make_finding(
            "t", classification="FIX", severity="low", detail="d",
        )
        expected_keys = {
            "type", "function", "line", "classification",
            "severity", "confidence", "detail",
        }
        self.assertEqual(set(f.keys()), expected_keys)


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
