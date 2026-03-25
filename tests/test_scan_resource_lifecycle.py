"""Tests for scan_resource_lifecycle.py — resource allocation/free tracking."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

lifecycle = import_script("scan_resource_lifecycle")


MALLOC_NEVER_FREED = """\
#include <Python.h>
#include <stdlib.h>

static PyObject *
leaky_malloc(PyObject *self, PyObject *args)
{
    char *buf = malloc(1024);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    /* BUG: buf is never freed */
    return PyUnicode_FromString(buf);
}
"""

MALLOC_FREED_CORRECTLY = """\
#include <Python.h>
#include <stdlib.h>

static PyObject *
good_malloc(PyObject *self, PyObject *args)
{
    char *buf = malloc(1024);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    PyObject *result = PyUnicode_FromString(buf);
    free(buf);
    return result;
}
"""

MALLOC_LEAK_ON_ERROR = """\
#include <Python.h>
#include <stdlib.h>

static PyObject *
error_leak(PyObject *self, PyObject *args)
{
    char *buf = malloc(1024);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    PyObject *result = PyUnicode_FromString(buf);
    if (result == NULL) {
        /* BUG: buf leaked on this error path */
        return NULL;
    }
    free(buf);
    return result;
}
"""

GOTO_CLEANUP = """\
#include <Python.h>
#include <stdlib.h>

static PyObject *
goto_cleanup(PyObject *self, PyObject *args)
{
    char *buf = malloc(1024);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    PyObject *result = PyUnicode_FromString(buf);
    if (result == NULL) {
        goto cleanup;
    }
    free(buf);
    return result;

cleanup:
    free(buf);
    return NULL;
}
"""

PYMEM_MALLOC = """\
#include <Python.h>

static PyObject *
pymem_leak(PyObject *self, PyObject *args)
{
    char *buf = PyMem_Malloc(256);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    /* BUG: PyMem_Free never called */
    return PyLong_FromLong(42);
}
"""

BUFFER_PROTOCOL = """\
#include <Python.h>

static PyObject *
buffer_leak(PyObject *self, PyObject *args)
{
    PyObject *obj;
    if (!PyArg_ParseTuple(args, "O", &obj))
        return NULL;

    Py_buffer view;
    int result = PyObject_GetBuffer(obj, &view, PyBUF_SIMPLE);
    if (result < 0)
        return NULL;

    /* BUG: PyBuffer_Release never called */
    return PyLong_FromLong(view.len);
}
"""

RESOURCE_RETURNED = """\
#include <Python.h>
#include <stdlib.h>

static char *
make_buffer(int size)
{
    char *buf = malloc(size);
    return buf;  /* Ownership transferred to caller */
}
"""


class TestScanResourceLifecycle(unittest.TestCase):
    """Test resource lifecycle tracking."""

    def test_malloc_never_freed(self):
        """Detect malloc'd buffer never freed."""
        with TempExtension({"ext.c": MALLOC_NEVER_FREED}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("resource_never_freed", types)
            finding = [f for f in result["findings"]
                       if f["type"] == "resource_never_freed"][0]
            self.assertEqual(finding["variable"], "buf")
            self.assertEqual(finding["alloc_func"], "malloc")

    def test_malloc_freed_no_finding(self):
        """Correctly freed malloc produces no finding."""
        with TempExtension({"ext.c": MALLOC_FREED_CORRECTLY}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            self.assertEqual(len(result["findings"]), 0)

    def test_malloc_leak_on_error_path(self):
        """Detect malloc leak on error return path."""
        with TempExtension({"ext.c": MALLOC_LEAK_ON_ERROR}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            leak_findings = [f for f in result["findings"]
                             if f["type"] == "resource_leak_on_error_path"]
            self.assertGreater(len(leak_findings), 0)
            self.assertEqual(leak_findings[0]["variable"], "buf")

    def test_goto_cleanup_no_finding(self):
        """Goto cleanup that frees the resource is not flagged."""
        with TempExtension({"ext.c": GOTO_CLEANUP}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            leak_findings = [f for f in result["findings"]
                             if f["type"] == "resource_leak_on_error_path"]
            self.assertEqual(len(leak_findings), 0)

    def test_pymem_malloc_detected(self):
        """Detect PyMem_Malloc without PyMem_Free."""
        with TempExtension({"ext.c": PYMEM_MALLOC}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("resource_never_freed", types)
            finding = [f for f in result["findings"]
                       if f["type"] == "resource_never_freed"][0]
            self.assertEqual(finding["alloc_func"], "PyMem_Malloc")

    def test_buffer_protocol_detected(self):
        """Detect PyObject_GetBuffer without PyBuffer_Release."""
        with TempExtension({"ext.c": BUFFER_PROTOCOL}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("resource_never_freed", types)

    def test_returned_resource_not_flagged(self):
        """Resource that is returned is not flagged as leaked."""
        with TempExtension({"ext.c": RESOURCE_RETURNED}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            never_freed = [f for f in result["findings"]
                           if f["type"] == "resource_never_freed"]
            self.assertEqual(len(never_freed), 0)

    def test_allocation_count(self):
        """Total tracked allocations are counted."""
        with TempExtension({"ext.c": MALLOC_LEAK_ON_ERROR}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            self.assertGreater(result["total_tracked_allocations"], 0)

    def test_output_envelope(self):
        """Output has correct structure."""
        with TempExtension({"ext.c": MINIMAL_EXTENSION}) as root:
            result = lifecycle.analyze(str(root / "ext.c"))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("total_tracked_allocations", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("skipped_files", result)


if __name__ == "__main__":
    unittest.main()
