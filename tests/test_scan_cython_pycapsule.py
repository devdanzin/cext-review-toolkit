"""Tests for scan_cython_pycapsule.py — Q3 PyCapsule_New NULL destructor.

Requires tree-sitter-cython (devdanzin fork).
"""

import unittest

from helpers import import_script, TempExtension

q3 = import_script("scan_cython_pycapsule")


# Bug fixture: capsules with NULL destructor.
NULL_DESTRUCTOR = """\
cdef object as_ffi_ptr(b2nd_array_t *arr):
    return PyCapsule_New(<void*>arr, "blosc2.b2nd_array_t", NULL)

cdef object schunk_capsule(blosc2_schunk *sc):
    return PyCapsule_New(<void*>sc, "blosc2.schunk", NULL)
"""

# Clean fixture: capsules with proper destructor.
WITH_DESTRUCTOR = """\
cdef void capsule_dtor(object cap) noexcept:
    pass

cdef object safe_capsule(void *ptr):
    return PyCapsule_New(ptr, "myname", capsule_dtor)
"""


class TestQ3Detection(unittest.TestCase):
    def test_null_destructor_flagged(self):
        with TempExtension({"x.pyx": NULL_DESTRUCTOR}) as root:
            result = q3.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 2)
        for f in result["findings"]:
            self.assertEqual(f["category"], "pycapsule_null_destructor")

    def test_with_destructor_not_flagged(self):
        with TempExtension({"x.pyx": WITH_DESTRUCTOR}) as root:
            result = q3.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_envelope_shape(self):
        with TempExtension({"x.pyx": NULL_DESTRUCTOR}) as root:
            result = q3.analyze(str(root / "x.pyx"))
        self.assertEqual(result["script"], "scan_cython_pycapsule")
        self.assertEqual(result["stats"]["files_scanned"], 1)
        self.assertEqual(result["stats"]["candidates"], 2)


if __name__ == "__main__":
    unittest.main()
