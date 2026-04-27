"""Tests for scan_cython_cdef_int_except.py — Q1 silent-noexcept detector.

Requires tree-sitter-cython (devdanzin fork).
"""

import unittest

from helpers import import_script, TempExtension

q1 = import_script("scan_cython_cdef_int_except")


# Bug fixture: cdef int callbacks without except clauses.
BUGGY = """\
cdef int callback_one(int x):
    raise RuntimeError("bad")
    return 0

cdef int callback_two(blosc2_codec_params *p):
    if p is NULL:
        raise RuntimeError("null")
    return 0

cdef Py_ssize_t weighted_size(int n):
    return n * 2
"""

# Clean fixture: explicit except / noexcept / pointer-return.
CLEAN = """\
cdef int safe_one(int x) except -1:
    return 0

cdef int safe_two() except *:
    return 0

cdef int safe_three(int x) noexcept:
    return 0

cdef int safe_four(int x) except? -1:
    return 0

cdef char** pointer_return(int n):
    return NULL

def python_function(x):
    return x
"""


class TestQ1Detection(unittest.TestCase):
    def test_buggy_cdef_int_flagged(self):
        with TempExtension({"buggy.pyx": BUGGY}) as root:
            result = q1.analyze(str(root / "buggy.pyx"))
        funcs = {f["function"] for f in result["findings"]}
        self.assertEqual(
            funcs,
            {"callback_one", "callback_two", "weighted_size"},
        )
        for f in result["findings"]:
            self.assertEqual(f["classification"], "FIX")
            self.assertEqual(f["confidence"], "HIGH")
            self.assertEqual(f["category"], "cdef_int_no_except")

    def test_clean_pyx_no_findings(self):
        with TempExtension({"clean.pyx": CLEAN}) as root:
            result = q1.analyze(str(root / "clean.pyx"))
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["stats"]["candidates"], 0)

    def test_pointer_return_excluded(self):
        # `cdef char** foo()` must NOT be flagged -- pointer-return semantics
        # differ from numeric-return silent-noexcept.
        src = "cdef char** alloc_array(int n):\n    return NULL\n"
        with TempExtension({"x.pyx": src}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_python_def_not_flagged(self):
        # Python `def` functions are not cdef_statements -- never flagged.
        src = "def foo(x):\n    return x\n"
        with TempExtension({"x.pyx": src}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_function_pointer_field_not_flagged(self):
        # cdef function-pointer field -- no body -- not a definition.
        src = "cdef struct S:\n    int (*cb)(int x)\n"
        with TempExtension({"x.pyx": src}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_unsigned_signed_modifiers(self):
        src = "cdef unsigned int counter():\n    return 0\n"
        with TempExtension({"x.pyx": src}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)


class TestQ1OutputShape(unittest.TestCase):
    def test_output_envelope_shape(self):
        with TempExtension({"x.pyx": BUGGY}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        self.assertEqual(result["script"], "scan_cython_cdef_int_except")
        self.assertIn("findings", result)
        self.assertIn("stats", result)
        for key in ("files_scanned", "candidates", "parse_errors"):
            self.assertIn(key, result["stats"])

    def test_finding_shape(self):
        with TempExtension({"x.pyx": BUGGY}) as root:
            result = q1.analyze(str(root / "x.pyx"))
        f = result["findings"][0]
        for key in (
            "file",
            "line",
            "column",
            "function",
            "category",
            "classification",
            "confidence",
            "description",
            "fix_template",
            "details",
        ):
            self.assertIn(key, f)
        self.assertIn("return_type", f["details"])
        self.assertIn("ast_layer", f["details"])
        self.assertIn("regex_layer", f["details"])


if __name__ == "__main__":
    unittest.main()
