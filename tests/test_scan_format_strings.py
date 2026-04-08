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
            finding = [
                f for f in result["findings"] if f["type"] == "format_string_mismatch"
            ][0]
            self.assertEqual(finding["api_call"], "PyArg_ParseTuple")
            self.assertEqual(finding["expected_args"], 2)
            self.assertEqual(finding["actual_args"], 1)

    def test_correct_pyarg_no_finding(self):
        """Correct PyArg_ParseTuple should not produce finding."""
        with TempExtension({"ext.c": CORRECT_PYARG}) as root:
            result = fmt.analyze(str(root / "ext.c"))
            mismatches = [
                f for f in result["findings"] if f["type"] == "format_string_mismatch"
            ]
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


class TestCountPyargFormatArgs(unittest.TestCase):
    """Unit tests for _count_pyarg_format_args format parser."""

    def _count(self, fmt_str):
        return fmt._count_pyarg_format_args(fmt_str)

    def test_simple_formats(self):
        self.assertEqual(self._count("i"), 1)
        self.assertEqual(self._count("id"), 2)
        self.assertEqual(self._count("ids"), 3)

    def test_O_bang(self):
        """O! consumes 2 args (type + object)."""
        self.assertEqual(self._count("O!si"), 4)

    def test_O_ampersand(self):
        """O& consumes 2 args (converter + void*)."""
        self.assertEqual(self._count("O&i"), 3)

    def test_es_hash(self):
        """es# consumes 3 args (encoding + buffer + length)."""
        self.assertEqual(self._count("es#"), 3)

    def test_et_hash(self):
        """et# consumes 3 args."""
        self.assertEqual(self._count("et#"), 3)

    def test_optional_separator(self):
        """| marks optional args but doesn't change count."""
        self.assertEqual(self._count("s|ii"), 3)

    def test_colon_stops(self):
        """: marks function name — stop counting."""
        self.assertEqual(self._count("s:funcname"), 1)

    def test_semicolon_stops(self):
        """; marks error message — stop counting."""
        self.assertEqual(self._count("si;bad args"), 2)

    def test_star_buffer(self):
        """y* and similar consume 1 arg (Py_buffer)."""
        self.assertEqual(self._count("y*"), 1)

    def test_hash_modifier(self):
        """s# consumes 2 args (char* + length)."""
        self.assertEqual(self._count("s#"), 2)

    def test_empty_format(self):
        self.assertEqual(self._count(""), 0)

    def test_parenthesized_tuple(self):
        """(ii) is a single tuple arg with 2 elements -> 2 addresses."""
        self.assertEqual(self._count("(ii)"), 2)

    def test_complex_format(self):
        """Complex real-world format string."""
        # "s(ffff)" = string + tuple of 4 floats = 5 addresses
        self.assertEqual(self._count("s(ffff)"), 5)


if __name__ == "__main__":
    unittest.main()
