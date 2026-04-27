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

# Shape B: blosc2-shape pointer-field. No __cinit__; __init__ allocates a
# raw C-pointer field that __dealloc__ owns. Should fire FIX HIGH.
POINTER_FIELD_LEAK = """\
cdef class SChunk:
    cdef blosc2_schunk *schunk
    cdef bint _is_view

    def __init__(self, ...):
        self.schunk = blosc2_schunk_new(storage)

    def __dealloc__(self):
        if self.schunk is not NULL:
            blosc2_schunk_free(self.schunk)
"""

# Shape B safe variant: __init__ checks NULL and frees before reassign.
POINTER_FIELD_SAFE_GUARD = """\
cdef class Safe:
    cdef blosc2_schunk *schunk

    def __init__(self, ...):
        if self.schunk is not NULL:
            blosc2_schunk_free(self.schunk)
        self.schunk = blosc2_schunk_new(storage)

    def __dealloc__(self):
        if self.schunk is not NULL:
            blosc2_schunk_free(self.schunk)
"""

# Shape B: borrowed view -- has pointer field but NO __dealloc__. Must NOT
# fire (the maintainer is signalling non-ownership).
BORROWED_VIEW = """\
cdef class vlmeta:
    cdef blosc2_schunk *schunk

    def __init__(self, schunk_ptr):
        self.schunk = <blosc2_schunk*> <uintptr_t> schunk_ptr
"""

# Shape B: pointer field but __init__'s RHS is a cast, not a call. Must NOT
# fire (no allocation happened).
POINTER_FIELD_CAST_ONLY = """\
cdef class CastOnly:
    cdef blosc2_schunk *schunk

    def __init__(self, raw):
        self.schunk = <blosc2_schunk*> raw

    def __dealloc__(self):
        pass
"""

# Shape B with a non-pointer field. Must NOT fire (Cython auto-manages).
NON_POINTER_FIELD = """\
cdef class Plain:
    cdef int flag

    def __init__(self):
        self.flag = compute_flag()

    def __dealloc__(self):
        pass
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

    def test_shape_a_marked_in_details(self):
        with TempExtension({"x.pyx": LEAKY}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"][0]["details"]["shape"], "overlap")


class TestQ4ShapeBPointerField(unittest.TestCase):
    """Shape B: blosc2-shape pointer-field reinit-leak (added in v2)."""

    def test_pointer_field_leak_flagged_high(self):
        with TempExtension({"x.pyx": POINTER_FIELD_LEAK}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        f = result["findings"][0]
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["confidence"], "HIGH")
        self.assertEqual(f["details"]["shape"], "pointer_field")
        self.assertEqual(f["details"]["field"], "schunk")
        self.assertEqual(f["details"]["field_type"], "blosc2_schunk *")
        self.assertEqual(f["details"]["class"], "SChunk")
        self.assertFalse(f["details"]["cinit_present"])

    def test_safe_guard_not_flagged(self):
        with TempExtension({"x.pyx": POINTER_FIELD_SAFE_GUARD}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_borrowed_view_not_flagged(self):
        # No __dealloc__ -> non-owning -> must skip
        with TempExtension({"x.pyx": BORROWED_VIEW}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_cast_only_not_flagged(self):
        # RHS is a cast, not a call -> not an allocation -> must skip
        with TempExtension({"x.pyx": POINTER_FIELD_CAST_ONLY}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_non_pointer_field_not_flagged(self):
        # Non-pointer field -> Cython manages -> must skip
        with TempExtension({"x.pyx": NON_POINTER_FIELD}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_shape_a_does_not_double_emit_under_b(self):
        # The cymem-shape LEAKY has overlap (Shape A). Even though `ptr` is
        # a `void*` pointer field with __dealloc__-style ownership, we must
        # only get one finding (Shape A wins; flagged_fields prevents B).
        # Note: LEAKY in this fixture doesn't declare a __dealloc__, so this
        # is implicitly tested -- but we can construct one that does.
        src = """\
cdef class Both:
    cdef void *ptr

    def __cinit__(self):
        self.ptr = NULL

    def __init__(self):
        self.ptr = malloc(10)

    def __dealloc__(self):
        if self.ptr is not NULL:
            free(self.ptr)
"""
        with TempExtension({"x.pyx": src}) as root:
            result = q4.analyze(str(root / "x.pyx"))
        # Should be exactly 1 finding (Shape A overlap), not 2
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["details"]["shape"], "overlap")


if __name__ == "__main__":
    unittest.main()
