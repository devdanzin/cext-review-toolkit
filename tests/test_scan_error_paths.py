"""Tests for scan_error_paths.py — error handling analysis."""

import pytest
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


def test_missing_null_check():
    """Detect missing NULL check after API call."""
    with TempExtension({"nocheck.c": MISSING_NULL_CHECK_CODE}) as root:
        result = error_paths.analyze(str(root / "nocheck.c"))
        types = [f["type"] for f in result["findings"]]
        assert "missing_null_check" in types


def test_unchecked_pyarg_parse():
    """Detect unchecked PyArg_ParseTuple."""
    with TempExtension({"parse.c": UNCHECKED_PARSE}) as root:
        result = error_paths.analyze(str(root / "parse.c"))
        types = [f["type"] for f in result["findings"]]
        assert "unchecked_pyarg_parse" in types


def test_clean_error_handling_minimal_findings():
    """Clean error handling produces no major findings."""
    with TempExtension({"clean.c": CLEAN_ERROR_HANDLING}) as root:
        result = error_paths.analyze(str(root / "clean.c"))
        unchecked = [f for f in result["findings"]
                     if f["type"] == "unchecked_pyarg_parse"]
        assert len(unchecked) == 0


def test_extension_with_bugs():
    """EXTENSION_WITH_BUGS should trigger findings."""
    with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
        result = error_paths.analyze(str(root / "buggy.c"))
        assert result["functions_analyzed"] >= 3
        assert len(result["findings"]) >= 1


def test_minimal_extension_runs():
    """Script runs without error on minimal extension."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = error_paths.analyze(str(root / "myext.c"))
        assert "findings" in result
        assert "summary" in result


def test_output_has_file_field():
    """Each finding has a file field."""
    with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
        result = error_paths.analyze(str(root / "buggy.c"))
        for f in result["findings"]:
            assert "file" in f
            assert "line" in f
            assert "confidence" in f


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


def test_return_without_exception():
    """Detect return NULL without setting an exception."""
    with TempExtension({"noexc.c": RETURN_WITHOUT_EXCEPTION}) as root:
        result = error_paths.analyze(str(root / "noexc.c"))
        types = [f["type"] for f in result["findings"]]
        assert "return_without_exception" in types


def test_exception_clobbering():
    """Detect Python API call in error handling block."""
    with TempExtension({"clobber.c": EXCEPTION_CLOBBERING}) as root:
        result = error_paths.analyze(str(root / "clobber.c"))
        types = [f["type"] for f in result["findings"]]
        assert "exception_clobbering" in types
