"""Tests for scan_cython_nogil_pyobject.py — Q5 nogil Python touch.

Requires tree-sitter-cython (devdanzin fork).
"""

import unittest

from helpers import import_script, TempExtension

q5 = import_script("scan_cython_nogil_pyobject")


# Bug fixture: Python ops inside cdef ... nogil: body without with-gil.
EVIL_NOGIL_FUNC = """\
cdef int evil(int x) nogil:
    raise ValueError('bad')
    print(x)
"""

# Safe fixture: same ops but inside `with gil:` block.
SAFE_WITH_GIL = """\
cdef int safe(int x) nogil:
    with gil:
        raise ValueError('bad')
        print(x)
"""

# Bug fixture: with nogil: block in a regular def.
WITH_NOGIL_BLOCK = """\
def caller():
    with nogil:
        raise ValueError('inside nogil block')
"""

# Mixed: nogil block with a nested with-gil block.
MIXED = """\
def caller():
    with nogil:
        raise ValueError('a')
        with gil:
            raise ValueError('b')
        print(\"after gil\")
"""

# Clean: GIL-held cdef function -- raises and prints are fine.
GIL_HELD = """\
cdef int gilly(int x):
    raise ValueError('ok')
    print(x)
"""

# F-string and comprehension inside nogil.
PYTHON_CONSTRUCTS = """\
cdef int with_fstring(int x) nogil:
    cdef str msg = f'value: {x}'
    return 0

cdef int with_comprehension() nogil:
    cdef list items = [i for i in range(10)]
    return 0
"""


class TestQ5Detection(unittest.TestCase):
    def test_evil_nogil_function_flagged(self):
        with TempExtension({"x.pyx": EVIL_NOGIL_FUNC}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        kinds = [f["details"]["kind"] for f in result["findings"]]
        self.assertIn("raise", kinds)
        self.assertIn("call to `print`", kinds)
        self.assertEqual(len(result["findings"]), 2)

    def test_raise_is_high_confidence(self):
        with TempExtension({"x.pyx": EVIL_NOGIL_FUNC}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        for f in result["findings"]:
            if f["details"]["kind"] == "raise":
                self.assertEqual(f["confidence"], "HIGH")

    def test_print_is_medium_confidence(self):
        with TempExtension({"x.pyx": EVIL_NOGIL_FUNC}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        for f in result["findings"]:
            if f["details"]["kind"] == "call to `print`":
                self.assertEqual(f["confidence"], "MEDIUM")

    def test_with_gil_block_protects(self):
        with TempExtension({"x.pyx": SAFE_WITH_GIL}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_with_nogil_block_flagged(self):
        with TempExtension({"x.pyx": WITH_NOGIL_BLOCK}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["details"]["kind"], "raise")

    def test_mixed_nogil_with_inner_gil(self):
        with TempExtension({"x.pyx": MIXED}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        # Should flag the outer raise + the print after the with-gil block,
        # but NOT the raise inside `with gil:`.
        kinds = [f["details"]["kind"] for f in result["findings"]]
        # 1 raise + 1 print = 2
        self.assertEqual(len(result["findings"]), 2)
        self.assertIn("raise", kinds)
        self.assertIn("call to `print`", kinds)

    def test_gil_held_cdef_not_flagged(self):
        with TempExtension({"x.pyx": GIL_HELD}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        self.assertEqual(result["findings"], [])

    def test_fstring_and_comprehension_flagged(self):
        with TempExtension({"x.pyx": PYTHON_CONSTRUCTS}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        kinds = {f["details"]["kind"] for f in result["findings"]}
        self.assertIn("f-string", kinds)
        self.assertIn("list comprehension", kinds)

    def test_envelope_shape(self):
        with TempExtension({"x.pyx": EVIL_NOGIL_FUNC}) as root:
            result = q5.analyze(str(root / "x.pyx"))
        self.assertEqual(result["script"], "scan_cython_nogil_pyobject")
        self.assertEqual(result["stats"]["files_scanned"], 1)


if __name__ == "__main__":
    unittest.main()
