"""Tests for scan_refcounts.py — reference counting analysis."""

import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_BUGS

refcounts = import_script("scan_refcounts")


LEAK_ON_ERROR = """\
#include <Python.h>

static PyObject *
leaky_error(PyObject *self, PyObject *args)
{
    PyObject *first = PyList_New(0);
    if (first == NULL)
        return NULL;

    PyObject *second = PyDict_New();
    if (second == NULL) {
        /* BUG: first is leaked here */
        return NULL;
    }
    Py_DECREF(first);
    Py_DECREF(second);
    Py_RETURN_NONE;
}
"""

CLEAN_REFCOUNTS = """\
#include <Python.h>

static PyObject *
clean_func(PyObject *self, PyObject *args)
{
    PyObject *result = PyList_New(0);
    if (result == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(result);
        return NULL;
    }

    if (PyList_Append(result, item) < 0) {
        Py_DECREF(item);
        Py_DECREF(result);
        return NULL;
    }
    Py_DECREF(item);
    return result;
}
"""

STOLEN_REF_CODE = """\
#include <Python.h>

static PyObject *
correct_steal(PyObject *self, PyObject *args)
{
    PyObject *list = PyList_New(1);
    if (list == NULL)
        return NULL;
    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(list);
        return NULL;
    }
    PyList_SetItem(list, 0, item);
    /* item is stolen — don't touch it */
    return list;
}
"""


def test_borrowed_ref_across_call():
    """Detect borrowed-ref-across-call in EXTENSION_WITH_BUGS."""
    with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
        result = refcounts.analyze(str(root / "buggy.c"))
        types = [f["type"] for f in result["findings"]]
        assert "borrowed_ref_across_call" in types
        borrow = [f for f in result["findings"]
                  if f["type"] == "borrowed_ref_across_call"][0]
        assert borrow["confidence"] == "high"
        assert "item" in borrow["borrowed_var"]


def test_clean_code_no_findings():
    """Clean code produces minimal or no refcount findings."""
    with TempExtension({"clean.c": CLEAN_REFCOUNTS}) as root:
        result = refcounts.analyze(str(root / "clean.c"))
        # Clean code should have no leaks or borrowed-ref issues.
        serious = [f for f in result["findings"]
                   if f["type"] in ("borrowed_ref_across_call", "stolen_ref_not_nulled")]
        assert len(serious) == 0


def test_leak_on_error_path():
    """Detect potential leak on error path."""
    with TempExtension({"leak.c": LEAK_ON_ERROR}) as root:
        result = refcounts.analyze(str(root / "leak.c"))
        types = [f["type"] for f in result["findings"]]
        assert "potential_leak_on_error" in types or "potential_leak" in types


def test_correct_stolen_ref():
    """Correct steal usage should not produce stolen_ref_not_nulled."""
    with TempExtension({"steal.c": STOLEN_REF_CODE}) as root:
        result = refcounts.analyze(str(root / "steal.c"))
        stolen = [f for f in result["findings"]
                  if f["type"] == "stolen_ref_not_nulled"]
        assert len(stolen) == 0


def test_minimal_extension_runs():
    """Script runs without error on minimal extension."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = refcounts.analyze(str(root / "myext.c"))
        assert result["functions_analyzed"] >= 2
        assert "findings" in result
        assert "summary" in result


def test_output_envelope():
    """Output has the correct envelope structure."""
    with TempExtension({"buggy.c": EXTENSION_WITH_BUGS}) as root:
        result = refcounts.analyze(str(root / "buggy.c"))
        assert "project_root" in result
        assert "scan_root" in result
        assert "functions_analyzed" in result
        assert "findings" in result
        assert "summary" in result
        assert "total_findings" in result["summary"]
        assert "by_type" in result["summary"]
