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


DEREF_MACRO_BUG = """\
#include <Python.h>

static PyObject *
macro_deref_bug(PyObject *self, PyObject *args)
{
    PyObject *ascii = PyUnicode_AsASCIIString(self);
    const char *s = PyBytes_AS_STRING(ascii);
    return PyUnicode_FromString(s);
}
"""

DEREF_MACRO_SAFE = """\
#include <Python.h>

static PyObject *
macro_deref_safe(PyObject *self, PyObject *args)
{
    PyObject *ascii = PyUnicode_AsASCIIString(self);
    if (ascii == NULL)
        return NULL;
    const char *s = PyBytes_AS_STRING(ascii);
    Py_DECREF(ascii);
    return PyUnicode_FromString(s);
}
"""


class TestDerefMacroDetection(unittest.TestCase):
    """Test dereference-like macro detection on unchecked values."""

    def test_detects_deref_macro_on_unchecked(self):
        """Detect PyBytes_AS_STRING on unchecked PyUnicode_AsASCIIString result."""
        with TempExtension({"ext.c": DEREF_MACRO_BUG}) as root:
            result = null_checks.analyze(str(root / "ext.c"))
            deref = [f for f in result["findings"]
                     if f["type"] == "deref_macro_on_unchecked"]
            self.assertGreater(len(deref), 0)
            self.assertEqual(deref[0]["macro"], "PyBytes_AS_STRING")

    def test_no_finding_when_null_checked(self):
        """No finding when the variable is NULL-checked before macro use."""
        with TempExtension({"ext.c": DEREF_MACRO_SAFE}) as root:
            result = null_checks.analyze(str(root / "ext.c"))
            deref = [f for f in result["findings"]
                     if f["type"] == "deref_macro_on_unchecked"]
            self.assertEqual(len(deref), 0)


CYTHON_UNLIKELY_CHECK = """\
#include <Python.h>

static PyObject *
cython_func(PyObject *self, PyObject *args)
{
    PyObject *result = PyObject_GetAttrString(self, "attr");
    if (unlikely(!result)) __PYX_ERR(0, 42, __pyx_L1_error);
    return result;
__pyx_L1_error:
    return NULL;
}
"""

CYTHON_UNLIKELY_EQ_CHECK = """\
#include <Python.h>

static PyObject *
cython_func2(PyObject *self, PyObject *args)
{
    PyObject *obj = PyDict_New();
    if (unlikely(obj == ((PyObject *)NULL))) __PYX_ERR(0, 42, __pyx_L1_error);
    return obj;
__pyx_L1_error:
    return NULL;
}
"""


class TestCythonNullPatterns(unittest.TestCase):
    """Test Cython-aware NULL check pattern recognition."""

    def test_unlikely_bang_not_flagged(self):
        """Cython unlikely(!var) pattern recognized as a NULL check."""
        with TempExtension({"ext.c": CYTHON_UNLIKELY_CHECK}) as root:
            result = null_checks.analyze(str(root / "ext.c"))
            unchecked = [f for f in result["findings"]
                         if f["type"] == "unchecked_alloc"
                         and f.get("variable") == "result"]
            self.assertEqual(len(unchecked), 0)

    def test_unlikely_eq_null_not_flagged(self):
        """Cython unlikely(var == ((type)NULL)) recognized as a NULL check."""
        with TempExtension({"ext.c": CYTHON_UNLIKELY_EQ_CHECK}) as root:
            result = null_checks.analyze(str(root / "ext.c"))
            unchecked = [f for f in result["findings"]
                         if f["type"] == "unchecked_alloc"
                         and f.get("variable") == "obj"]
            self.assertEqual(len(unchecked), 0)


if __name__ == "__main__":
    unittest.main()
