"""Tests for scan_null_checks.py — NULL safety analysis."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_BUGS

null_checks = import_script("scan_null_checks")


UNCHECKED_ALLOC = """\
#include <Python.h>

static PyObject *
bad_alloc(PyObject *self, PyObject *args)
{
    char *buf = (char *)PyMem_Malloc(1024);
    buf[0] = 'x';  /* no NULL check! */
    PyMem_Free(buf);
    Py_RETURN_NONE;
}
"""

CHECKED_ALLOC = """\
#include <Python.h>

static PyObject *
good_alloc(PyObject *self, PyObject *args)
{
    char *buf = (char *)PyMem_Malloc(1024);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    buf[0] = 'x';
    PyMem_Free(buf);
    Py_RETURN_NONE;
}
"""

DEREF_BEFORE_CHECK = """\
#include <Python.h>

static PyObject *
deref_first(PyObject *self, PyObject *args)
{
    PyObject *obj = PyDict_GetItemString(NULL, "key");
    int x = obj->ob_refcnt;
    if (obj == NULL)
        return NULL;
    return PyLong_FromLong(x);
}
"""


class TestScanNullChecks(unittest.TestCase):
    """Test NULL safety analysis."""

    def test_unchecked_alloc(self):
        """Detect unchecked allocation."""
        with TempExtension({"alloc.c": UNCHECKED_ALLOC}) as root:
            result = null_checks.analyze(str(root / "alloc.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("unchecked_alloc", types)

    def test_checked_alloc_no_finding(self):
        """Checked allocation should not trigger unchecked_alloc."""
        with TempExtension({"good.c": CHECKED_ALLOC}) as root:
            result = null_checks.analyze(str(root / "good.c"))
            unchecked = [f for f in result["findings"]
                         if f["type"] == "unchecked_alloc"]
            self.assertEqual(len(unchecked), 0)

    def test_deref_before_check(self):
        """Detect dereference before NULL check."""
        with TempExtension({"deref.c": DEREF_BEFORE_CHECK}) as root:
            result = null_checks.analyze(str(root / "deref.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("deref_before_check", types)

    def test_extension_with_bugs(self):
        """EXTENSION_WITH_BUGS should trigger null check findings."""
        with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
            result = null_checks.analyze(str(root / "buggy.c"))
            self.assertGreaterEqual(result["functions_analyzed"], 3)

    def test_minimal_extension_runs(self):
        """Script runs without error on minimal extension."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = null_checks.analyze(str(root / "myext.c"))
            self.assertIn("findings", result)
            self.assertIn("summary", result)


if __name__ == "__main__":
    unittest.main()
