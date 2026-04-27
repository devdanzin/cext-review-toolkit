"""Tests for scan_cython_buffer_protocol.py — Q2 PyObject_GetBuffer pairing.

Requires tree-sitter-cython (devdanzin fork).
"""

import unittest

from helpers import import_script, TempExtension

q2 = import_script("scan_cython_buffer_protocol")


# Reference correct pattern: GetBuffer + Release in try/finally.
SAFE = """\
cdef int compress_safe(object src):
    cdef Py_buffer buf
    if PyObject_GetBuffer(src, &buf, PyBUF_SIMPLE) < 0:
        return -1
    try:
        return _do_work(&buf)
    finally:
        PyBuffer_Release(&buf)
"""

# MEDIUM: Release exists but is NOT in finally -- can leak on exception.
RELEASE_NOT_IN_FINALLY = """\
cdef int compress_unsafe(object src):
    cdef Py_buffer buf
    if PyObject_GetBuffer(src, &buf, PyBUF_SIMPLE) < 0:
        return -1
    _do_work(&buf)
    PyBuffer_Release(&buf)
    return 0
"""

# HIGH: no Release anywhere in the function.
NO_RELEASE = """\
cdef int compress_leaky(object src):
    cdef Py_buffer buf
    if PyObject_GetBuffer(src, &buf, PyBUF_SIMPLE) < 0:
        return -1
    return _do_work(&buf)
"""


class TestQ2Detection(unittest.TestCase):
    def test_safe_pairing_not_flagged(self):
        with TempExtension({"x.pyx": SAFE}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_release_not_in_finally_medium(self):
        with TempExtension({"x.pyx": RELEASE_NOT_IN_FINALLY}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        f = result["findings"][0]
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["confidence"], "MEDIUM")
        self.assertTrue(f["details"]["any_release_present"])
        self.assertFalse(f["details"]["in_finally"])

    def test_no_release_high(self):
        with TempExtension({"x.pyx": NO_RELEASE}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        f = result["findings"][0]
        self.assertEqual(f["confidence"], "HIGH")
        self.assertFalse(f["details"]["any_release_present"])

    def test_buffer_var_extracted(self):
        with TempExtension({"x.pyx": NO_RELEASE}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"][0]["details"]["buffer_var"], "buf")

    def test_function_name_attached(self):
        with TempExtension({"x.pyx": NO_RELEASE}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"][0]["function"], "compress_leaky")

    def test_envelope_shape(self):
        with TempExtension({"x.pyx": SAFE}) as root:
            result = q2.analyze(str(root / "x.pyx"))
        self.assertEqual(result["script"], "scan_cython_buffer_protocol")
        self.assertEqual(result["stats"]["files_scanned"], 1)


if __name__ == "__main__":
    unittest.main()
