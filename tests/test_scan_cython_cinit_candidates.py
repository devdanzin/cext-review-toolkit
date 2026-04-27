"""Tests for scan_cython_cinit_candidates.py — Q4 __cinit__/__init__ reinit-leak.

Requires tree-sitter-cython (devdanzin fork).
"""

import unittest

from helpers import import_script, TempExtension

q4 = import_script("scan_cython_cinit_candidates")


# Bug fixture: __init__ reassigns a field __cinit__ already populated, with
# no intervening free. RHS is a function call -> HIGH confidence.
LEAKY = """\
cdef class Address:
    cdef void* ptr
    cdef object mem

    def __cinit__(self, size_t n):
        self.mem = Pool()
        self.ptr = self.mem.alloc(n)

    def __init__(self, size_t n):
        self.ptr = self.mem.alloc(n)
"""

# Safe fixture: __init__ frees old value before reassigning.
SAFE = """\
cdef class Safe:
    cdef void* ptr

    def __cinit__(self):
        self.ptr = malloc(10)

    def __init__(self):
        if self.ptr is not NULL:
            free(self.ptr)
        self.ptr = malloc(20)
"""

# No overlap: __cinit__ and __init__ assign different fields.
NO_OVERLAP = """\
cdef class NoOverlap:
    def __cinit__(self):
        self.a = 1
    def __init__(self):
        self.b = 2
"""

# Only one of the two methods present.
ONLY_CINIT = """\
cdef class OnlyCinit:
    def __cinit__(self):
        self.foo = 1
"""

# RHS is not a function call -> MEDIUM (CONSIDER) confidence.
INTEGER_RHS = """\
cdef class WithInteger:
    def __cinit__(self):
        self.flag = 0
    def __init__(self):
        self.flag = 1
"""


class TestQ4Detection(unittest.TestCase):
    def test_leaky_high_confidence(self):
        with TempExtension({"x.pyx": LEAKY}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        f = result["findings"][0]
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["confidence"], "HIGH")
        self.assertEqual(f["category"], "cinit_init_reinit_leak")
        self.assertEqual(f["details"]["field"], "ptr")
        self.assertEqual(f["details"]["class"], "Address")
        self.assertTrue(f["details"]["rhs_is_call"])

    def test_safe_with_free_not_flagged(self):
        with TempExtension({"x.pyx": SAFE}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_no_overlap_not_flagged(self):
        with TempExtension({"x.pyx": NO_OVERLAP}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_only_cinit_not_flagged(self):
        with TempExtension({"x.pyx": ONLY_CINIT}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_integer_rhs_medium_confidence(self):
        with TempExtension({"x.pyx": INTEGER_RHS}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        f = result["findings"][0]
        self.assertEqual(f["confidence"], "MEDIUM")
        self.assertEqual(f["classification"], "CONSIDER")
        self.assertFalse(f["details"]["rhs_is_call"])

    def test_function_label_includes_class(self):
        with TempExtension({"x.pyx": LEAKY}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"][0]["function"], "Address.__init__")

    def test_envelope_shape(self):
        with TempExtension({"x.pyx": LEAKY}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["script"], "scan_cython_cinit_candidates")
        self.assertEqual(result["stats"]["files_scanned"], 1)


if __name__ == "__main__":
    unittest.main()
