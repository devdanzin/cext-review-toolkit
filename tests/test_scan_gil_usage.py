"""Tests for scan_gil_usage.py — GIL discipline analysis."""

import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

gil = import_script("scan_gil_usage")


MISMATCHED_GIL = """\
#include <Python.h>

static PyObject *
bad_gil(PyObject *self, PyObject *args)
{
    Py_BEGIN_ALLOW_THREADS
    sleep(1);
    /* missing Py_END_ALLOW_THREADS */
    return Py_None;
}
"""

API_WITHOUT_GIL = """\
#include <Python.h>

static PyObject *
api_no_gil(PyObject *self, PyObject *args)
{
    Py_BEGIN_ALLOW_THREADS
    PyObject *obj = PyLong_FromLong(42);
    Py_END_ALLOW_THREADS
    return obj;
}
"""

BLOCKING_WITH_GIL = """\
#include <Python.h>
#include <unistd.h>

static PyObject *
blocking_func(PyObject *self, PyObject *args)
{
    PyObject *result = PyLong_FromLong(1);
    sleep(5);
    return result;
}
"""

CORRECT_GIL = """\
#include <Python.h>

static PyObject *
correct_func(PyObject *self, PyObject *args)
{
    int result;
    Py_BEGIN_ALLOW_THREADS
    result = some_blocking_call();
    Py_END_ALLOW_THREADS
    return PyLong_FromLong(result);
}
"""

GLOBAL_PYOBJECT = """\
#include <Python.h>

static PyObject *cache = NULL;

static PyObject *
get_cache(PyObject *self, PyObject *args)
{
    if (cache == NULL) {
        cache = PyDict_New();
    }
    Py_INCREF(cache);
    return cache;
}
"""


def test_mismatched_allow_threads():
    """Detect mismatched BEGIN/END_ALLOW_THREADS."""
    with TempExtension({"bad.c": MISMATCHED_GIL}) as root:
        result = gil.analyze(str(root / "bad.c"))
        types = [f["type"] for f in result["findings"]]
        assert "mismatched_allow_threads" in types


def test_api_without_gil():
    """Detect Python API call in GIL-released region."""
    with TempExtension({"api.c": API_WITHOUT_GIL}) as root:
        result = gil.analyze(str(root / "api.c"))
        types = [f["type"] for f in result["findings"]]
        assert "api_without_gil" in types


def test_blocking_with_gil():
    """Detect blocking call with GIL held."""
    with TempExtension({"block.c": BLOCKING_WITH_GIL}) as root:
        result = gil.analyze(str(root / "block.c"))
        types = [f["type"] for f in result["findings"]]
        assert "blocking_with_gil" in types


def test_free_threading_concern():
    """Detect static mutable PyObject* as free-threading concern."""
    with TempExtension({"global.c": GLOBAL_PYOBJECT}) as root:
        result = gil.analyze(str(root / "global.c"))
        types = [f["type"] for f in result["findings"]]
        assert "free_threading_concern" in types


CALLBACK_WITHOUT_GIL = """\
#include <Python.h>

static void
my_callback(void *data)
{
    PyObject *result = PyLong_FromLong(42);
    Py_XDECREF(result);
}

static PyObject *
setup_callback(PyObject *self, PyObject *args)
{
    register_handler(my_callback, NULL);
    Py_RETURN_NONE;
}
"""


def test_callback_without_gil():
    """Detect callback function calling Python APIs without GIL."""
    with TempExtension({"cb.c": CALLBACK_WITHOUT_GIL}) as root:
        result = gil.analyze(str(root / "cb.c"))
        types = [f["type"] for f in result["findings"]]
        assert "callback_without_gil" in types
        cb_findings = [f for f in result["findings"]
                       if f["type"] == "callback_without_gil"]
        assert cb_findings[0]["function"] == "my_callback"


def test_minimal_extension_runs():
    """Script runs without error on minimal extension."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = gil.analyze(str(root / "myext.c"))
        assert "findings" in result
        assert "summary" in result
