"""Tests for scan_pyerr_clear.py — PyErr_Clear audit."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

pyerr_clear = import_script("scan_pyerr_clear")


UNGUARDED_CLEAR = """\
#include <Python.h>

static PyObject *
bad_clear(PyObject *self, PyObject *args)
{
    PyObject *result = PyDict_GetItem(self, args);
    if (result == NULL) {
        PyErr_Clear();  /* BAD: swallows MemoryError */
        Py_RETURN_NONE;
    }
    Py_INCREF(result);
    return result;
}
"""

GUARDED_CLEAR = """\
#include <Python.h>

static PyObject *
good_clear(PyObject *self, PyObject *args)
{
    PyObject *result = PyDict_GetItem(self, args);
    if (result == NULL) {
        if (PyErr_ExceptionMatches(PyExc_KeyError)) {
            PyErr_Clear();
            Py_RETURN_NONE;
        }
        return NULL;  /* propagate other exceptions */
    }
    Py_INCREF(result);
    return result;
}
"""

GUARDED_WITH_FETCH = """\
#include <Python.h>

static PyObject *
fetch_clear(PyObject *self, PyObject *args)
{
    PyObject *type, *value, *tb;
    PyErr_Fetch(&type, &value, &tb);
    /* Intentional: inspecting the exception */
    PyErr_Clear();
    Py_XDECREF(type);
    Py_XDECREF(value);
    Py_XDECREF(tb);
    Py_RETURN_NONE;
}
"""

HOT_PATH_CLEAR = """\
#include <Python.h>

static PyObject *
MyObj_getitem(PyObject *self, PyObject *key)
{
    PyObject *result = PyDict_GetItem(self, key);
    if (result == NULL) {
        PyErr_Clear();  /* BAD: hot path + swallows MemoryError */
        Py_RETURN_NONE;
    }
    Py_INCREF(result);
    return result;
}
"""

MULTIPLE_CLEARS = """\
#include <Python.h>

static PyObject *
multi_clear(PyObject *self, PyObject *args)
{
    PyObject *a = PyObject_GetAttrString(self, "a");
    if (a == NULL) {
        PyErr_Clear();
    }

    PyObject *b = PyObject_GetAttrString(self, "b");
    if (b == NULL) {
        if (PyErr_ExceptionMatches(PyExc_AttributeError)) {
            PyErr_Clear();
        } else {
            return NULL;
        }
    }

    Py_RETURN_NONE;
}
"""

NO_CLEAR = """\
#include <Python.h>

static PyObject *
clean_function(PyObject *self, PyObject *args)
{
    PyObject *result = PyLong_FromLong(42);
    if (result == NULL)
        return NULL;
    return result;
}
"""


class TestScanPyErrClear(unittest.TestCase):
    """Test PyErr_Clear audit scanner."""

    def test_unguarded_clear_detected(self):
        """Unguarded PyErr_Clear is flagged."""
        with TempExtension({"ext.c": UNGUARDED_CLEAR}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("unguarded_pyerr_clear", types)

    def test_guarded_clear_not_flagged(self):
        """PyErr_Clear preceded by ExceptionMatches is not flagged."""
        with TempExtension({"ext.c": GUARDED_CLEAR}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            self.assertEqual(len(result["findings"]), 0)

    def test_fetch_guard_not_flagged(self):
        """PyErr_Clear preceded by PyErr_Fetch is not flagged."""
        with TempExtension({"ext.c": GUARDED_WITH_FETCH}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            self.assertEqual(len(result["findings"]), 0)

    def test_hot_path_higher_severity(self):
        """Unguarded clear in hot path function has higher severity."""
        with TempExtension({"ext.c": HOT_PATH_CLEAR}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("broad_pyerr_clear_in_hot_path", types)
            hot = [f for f in result["findings"]
                   if f["type"] == "broad_pyerr_clear_in_hot_path"]
            self.assertEqual(hot[0]["confidence"], "high")

    def test_multiple_clears_mixed(self):
        """Multiple clears: only unguarded ones flagged."""
        with TempExtension({"ext.c": MULTIPLE_CLEARS}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            # First clear is unguarded, second is guarded
            self.assertEqual(len(result["findings"]), 1)
            self.assertEqual(result["findings"][0]["function"], "multi_clear")

    def test_no_clear_no_findings(self):
        """Code without PyErr_Clear has no findings."""
        with TempExtension({"ext.c": NO_CLEAR}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            self.assertEqual(len(result["findings"]), 0)
            self.assertEqual(result["total_pyerr_clear_calls"], 0)

    def test_total_clears_counted(self):
        """Total PyErr_Clear calls are counted correctly."""
        with TempExtension({"ext.c": MULTIPLE_CLEARS}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            self.assertEqual(result["total_pyerr_clear_calls"], 2)

    def test_output_envelope(self):
        """Output has correct structure."""
        with TempExtension({"ext.c": MINIMAL_EXTENSION}) as root:
            result = pyerr_clear.analyze(str(root / "ext.c"))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("total_pyerr_clear_calls", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("skipped_files", result)


if __name__ == "__main__":
    unittest.main()
