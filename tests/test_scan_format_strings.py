"""Tests for scan_format_strings.py — format string validation."""

import unittest
from helpers import import_script, TempExtension

fmt = import_script("scan_format_strings")


MISMATCH_PYARG = """\
#include <Python.h>

static PyObject *
wrong_count(PyObject *self, PyObject *args)
{
    int a;
    double b;
    /* Format expects 2 args (i, d) but only 1 address provided */
    if (!PyArg_ParseTuple(args, "id", &a))
        return NULL;
    Py_RETURN_NONE;
}
"""

CORRECT_PYARG = """\
#include <Python.h>

static PyObject *
correct_count(PyObject *self, PyObject *args)
{
    int a;
    double b;
    if (!PyArg_ParseTuple(args, "id", &a, &b))
        return NULL;
    Py_RETURN_NONE;
}
"""

BUILD_VALUE_MISMATCH = """\
#include <Python.h>

static PyObject *
wrong_build(PyObject *self, PyObject *args)
{
    /* Format expects 3 args (i, s, f) but only 2 provided */
    return Py_BuildValue("isf", 42, "hello");
}
"""

PRINTF_FORMAT = """\
#include <Python.h>

static PyObject *
wrong_format(PyObject *self, PyObject *args)
{
    /* Format expects 2 args (%s, %d) but 3 provided */
    PyErr_Format(PyExc_TypeError, "got %s and %d", "hello", 42, 99);
    return NULL;
}
"""


class TestScanFormatStrings(unittest.TestCase):
    """Test format string mismatch detection."""

    def test_detects_pyarg_mismatch(self):
        """Detect wrong arg count in PyArg_ParseTuple."""
        with TempExtension({"ext.c": MISMATCH_PYARG}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("format_string_mismatch", types)
            finding = [f for f in result["findings"]
                       if f["type"] == "format_string_mismatch"][0]
            self.assertEqual(finding["api_call"], "PyArg_ParseTuple")
            self.assertEqual(finding["expected_args"], 2)
            self.assertEqual(finding["actual_args"], 1)

    def test_correct_pyarg_no_finding(self):
        """Correct PyArg_ParseTuple should not produce finding."""
        with TempExtension({"ext.c": CORRECT_PYARG}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            mismatches = [f for f in result["findings"]
                          if f["type"] == "format_string_mismatch"]
            self.assertEqual(len(mismatches), 0)

    def test_detects_build_value_mismatch(self):
        """Detect wrong arg count in Py_BuildValue."""
        with TempExtension({"ext.c": BUILD_VALUE_MISMATCH}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("format_string_mismatch", types)

    def test_detects_printf_format_mismatch(self):
        """Detect wrong arg count in PyErr_Format."""
        with TempExtension({"ext.c": PRINTF_FORMAT}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("format_string_mismatch", types)

    def test_output_envelope(self):
        """Output has correct structure."""
        with TempExtension({"ext.c": CORRECT_PYARG}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)


if __name__ == "__main__":
    unittest.main()
