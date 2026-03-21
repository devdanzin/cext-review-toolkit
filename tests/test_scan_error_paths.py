"""Tests for scan_error_paths.py — error handling analysis."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_BUGS

error_paths = import_script("scan_error_paths")


MISSING_NULL_CHECK_CODE = """\
#include <Python.h>

static PyObject *
no_check(PyObject *self, PyObject *args)
{
    PyObject *obj = PyObject_GetAttrString(self, "missing");
    PyObject *result = PyObject_Str(obj);
    return result;
}
"""

UNCHECKED_PARSE = """\
#include <Python.h>

static PyObject *
bad_parse(PyObject *self, PyObject *args)
{
    int x;
    PyArg_ParseTuple(args, "i", &x);
    return PyLong_FromLong(x);
}
"""

CLEAN_ERROR_HANDLING = """\
#include <Python.h>

static PyObject *
clean_func(PyObject *self, PyObject *args)
{
    int x;
    if (!PyArg_ParseTuple(args, "i", &x))
        return NULL;
    PyObject *result = PyLong_FromLong(x);
    if (result == NULL)
        return NULL;
    return result;
}
"""

RETURN_WITHOUT_EXCEPTION = """\
#include <Python.h>

static PyObject *
no_exception(PyObject *self, PyObject *args)
{
    int x = 42;
    if (x > 100)
        return NULL;
    Py_RETURN_NONE;
}
"""

EXCEPTION_CLOBBERING = """\
#include <Python.h>

static PyObject *
clobber(PyObject *self, PyObject *args)
{
    PyObject *a = PyList_New(0);
    if (a == NULL) {
        PyObject *fallback = PyDict_New();
        return fallback;
    }
    return a;
}
"""


class TestScanErrorPaths(unittest.TestCase):
    """Test error handling bug detection."""

    def test_missing_null_check(self):
        """Detect missing NULL check after API call."""
        with TempExtension({"nocheck.c": MISSING_NULL_CHECK_CODE}) as root:
            result = error_paths.analyze(str(root / "nocheck.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("missing_null_check", types)

    def test_unchecked_pyarg_parse(self):
        """Detect unchecked PyArg_ParseTuple."""
        with TempExtension({"parse.c": UNCHECKED_PARSE}) as root:
            result = error_paths.analyze(str(root / "parse.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("unchecked_pyarg_parse", types)

    def test_clean_error_handling_minimal_findings(self):
        """Clean error handling produces no major findings."""
        with TempExtension({"clean.c": CLEAN_ERROR_HANDLING}) as root:
            result = error_paths.analyze(str(root / "clean.c"))
            unchecked = [f for f in result["findings"]
                         if f["type"] == "unchecked_pyarg_parse"]
            self.assertEqual(len(unchecked), 0)

    def test_extension_with_bugs(self):
        """EXTENSION_WITH_BUGS should trigger findings."""
        with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
            result = error_paths.analyze(str(root / "buggy.c"))
            self.assertGreaterEqual(result["functions_analyzed"], 3)
            self.assertGreaterEqual(len(result["findings"]), 1)

    def test_minimal_extension_runs(self):
        """Script runs without error on minimal extension."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = error_paths.analyze(str(root / "myext.c"))
            self.assertIn("findings", result)
            self.assertIn("summary", result)

    def test_output_has_file_field(self):
        """Each finding has a file field."""
        with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
            result = error_paths.analyze(str(root / "buggy.c"))
            for f in result["findings"]:
                self.assertIn("file", f)
                self.assertIn("line", f)
                self.assertIn("confidence", f)

    def test_return_without_exception(self):
        """Detect return NULL without setting an exception."""
        with TempExtension({"noexc.c": RETURN_WITHOUT_EXCEPTION}) as root:
            result = error_paths.analyze(str(root / "noexc.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("return_without_exception", types)

    def test_exception_clobbering(self):
        """Detect Python API call in error handling block."""
        with TempExtension({"clobber.c": EXCEPTION_CLOBBERING}) as root:
            result = error_paths.analyze(str(root / "clobber.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("exception_clobbering", types)


if __name__ == "__main__":
    unittest.main()
