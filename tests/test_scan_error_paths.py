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

NON_ERRORING_IN_ERROR_BLOCK = """\
#include <Python.h>

/* PyUnicode_CompareWithASCIIString is documented as 'does not raise
 * exceptions'. Calling it inside an error-handling block cannot clobber
 * the pending exception, so this should NOT produce an
 * exception_clobbering finding. */
static PyObject *
not_clobbering(PyObject *self, PyObject *value)
{
    PyObject *a = PyList_New(0);
    if (a == NULL) {
        if (PyUnicode_CompareWithASCIIString(value, "fallback") == 0) {
            PyErr_SetString(PyExc_TypeError, "fallback rejected");
        }
        return NULL;
    }
    return a;
}
"""

NO_EXCEPTION_MACROS_IN_ERROR_BLOCK = """\
#include <Python.h>

/* Refcount macros, type-check macros, and exception inspection APIs
 * cannot raise exceptions and therefore cannot clobber a pending one.
 * None of these calls in the error block should produce
 * exception_clobbering findings. */
static PyObject *
not_clobbering_macros(PyObject *self, PyObject *value)
{
    PyObject *a = PyList_New(0);
    if (a == NULL) {
        /* Refcount macros — never raise. */
        Py_INCREF(value);
        /* Exception inspection — read-only, never raises. */
        if (PyErr_ExceptionMatches(PyExc_MemoryError)) {
            /* Type-check macros — read tp_flags, never raise. */
            if (PyCFunction_Check(value)) {
                return NULL;
            }
            /* Type access macros — read ob_type, never raise. */
            if (Py_TYPE(value) == &PyLong_Type) {
                return NULL;
            }
        }
        return NULL;
    }
    return a;
}
"""

EXCEPTION_STATE_AND_GC_IN_ERROR_BLOCK = """\
#include <Python.h>

/* PyErr_Fetch / PyErr_Restore / PyErr_Get/SetRaisedException manage the
 * pending-exception state itself — they cannot set a *new* exception
 * because by definition that's what they operate on. PyObject_GC_Track
 * and PyObject_GC_UnTrack are GC bookkeeping operations that only flip
 * a flag on the GC header; they cannot raise. None of the calls in the
 * error block below should produce exception_clobbering findings.
 *
 * Regression for the 2026-04-14 sweep: msgspec had 16 false positives
 * from PyObject_GC_Track (8), PyErr_Fetch (4), PyErr_Restore (4) before
 * the allowlist was extended. */
static PyObject *
state_and_gc(PyObject *self, PyObject *obj)
{
    PyObject *a = PyList_New(0);
    if (a == NULL) {
        /* Save and restore the pending exception. */
        PyObject *exc_type, *exc_value, *exc_tb;
        PyErr_Fetch(&exc_type, &exc_value, &exc_tb);
        /* GC tracking — pure bookkeeping, cannot raise. */
        PyObject_GC_UnTrack(obj);
        PyErr_Restore(exc_type, exc_value, exc_tb);
        return NULL;
    }
    PyObject_GC_Track(a);
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

    def test_non_erroring_api_not_flagged_as_clobber(self):
        """Calls to documented non-erroring APIs inside an error block
        must not produce exception_clobbering findings.

        Regression test for wrapt v2 re-audit false positives #19 and #33
        (2026-04-12): PyUnicode_CompareWithASCIIString was flagged as a
        possible exception clobberer even though its CPython header
        explicitly states 'This function does not raise exceptions'.
        """
        with TempExtension({"ok.c": NON_ERRORING_IN_ERROR_BLOCK}) as root:
            result = error_paths.analyze(str(root / "ok.c"))
            clobber_findings = [
                f for f in result["findings"]
                if f["type"] == "exception_clobbering"
                and f.get("api_call") == "PyUnicode_CompareWithASCIIString"
            ]
            self.assertEqual(
                clobber_findings, [],
                "PyUnicode_CompareWithASCIIString should not be flagged as "
                "exception_clobbering; it does not raise exceptions "
                "(Include/unicodeobject.h:957).",
            )

    def test_no_exception_macros_not_flagged_as_clobber(self):
        """Refcount macros, type-check macros, and exception-inspection
        APIs called inside an error block must not produce
        exception_clobbering findings. They cannot set an exception and
        therefore cannot clobber a pending one.

        Regression for the 21 false positives observed on wrapt
        (Py_INCREF x10, PyErr_ExceptionMatches x9, Py_TYPE x1,
        PyCFunction_Check x1) during the 2026-04-12 re-audit.
        """
        with TempExtension({"ok.c": NO_EXCEPTION_MACROS_IN_ERROR_BLOCK}) as root:
            result = error_paths.analyze(str(root / "ok.c"))
            false_positive_apis = {
                "Py_INCREF", "PyErr_ExceptionMatches",
                "PyCFunction_Check", "Py_TYPE",
            }
            leaked = [
                f for f in result["findings"]
                if f["type"] == "exception_clobbering"
                and f.get("api_call") in false_positive_apis
            ]
            self.assertEqual(
                leaked, [],
                f"The following no-exception APIs leaked through the filter: "
                f"{[f.get('api_call') for f in leaked]}. "
                f"They should be listed in "
                f"data/api_tables.json 'no_exception_apis'.",
            )

    def test_exception_state_and_gc_apis_not_flagged_as_clobber(self):
        """PyErr_Fetch/Restore/Get/SetRaisedException manage the pending
        exception itself and cannot set a new one. PyObject_GC_Track and
        PyObject_GC_UnTrack are GC bookkeeping and cannot raise. None of
        these should produce exception_clobbering findings.

        Regression for the 16 false positives observed on msgspec's
        _core.c during the 2026-04-14 sweep (PyObject_GC_Track x8,
        PyErr_Fetch x4, PyErr_Restore x4), which dropped msgspec's
        exception_clobbering count from 88 to 72.
        """
        with TempExtension({"ok.c": EXCEPTION_STATE_AND_GC_IN_ERROR_BLOCK}) as root:
            result = error_paths.analyze(str(root / "ok.c"))
            false_positive_apis = {
                "PyErr_Fetch", "PyErr_Restore",
                "PyObject_GC_Track", "PyObject_GC_UnTrack",
            }
            leaked = [
                f for f in result["findings"]
                if f["type"] == "exception_clobbering"
                and f.get("api_call") in false_positive_apis
            ]
            self.assertEqual(
                leaked, [],
                f"The following exception-state/GC-track APIs leaked through "
                f"the filter: {[f.get('api_call') for f in leaked]}. "
                f"They should be listed in "
                f"data/api_tables.json 'no_exception_apis' under "
                f"'exception_state_management' or 'gc_tracking'.",
            )


if __name__ == "__main__":
    unittest.main()
