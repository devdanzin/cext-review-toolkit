"""Tests for scan_gil_usage.py — GIL discipline analysis."""

import unittest
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


TYPE_SLOT_NOT_FLAGGED = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} MyObj;

static void
MyObj_dealloc(MyObj *self)
{
    Py_XDECREF(self->data);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
MyObj_traverse(MyObj *self, visitproc visit, void *arg)
{
    Py_VISIT(self->data);
    return 0;
}

static PyObject *
setup_type(PyObject *self, PyObject *args)
{
    /* Pass dealloc as function pointer — should NOT trigger callback_without_gil */
    PyTypeObject type = {0};
    type.tp_dealloc = (destructor)MyObj_dealloc;
    type.tp_traverse = (traverseproc)MyObj_traverse;
    PyType_Ready(&type);
    Py_RETURN_NONE;
}

static PyTypeObject MyObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mymod.MyObj",
    .tp_basicsize = sizeof(MyObj),
    .tp_dealloc = (destructor)MyObj_dealloc,
    .tp_traverse = (traverseproc)MyObj_traverse,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
};
"""

TYPE_SLOT_SPEC_NOT_FLAGGED = """\
#include <Python.h>

static void
MyObj_dealloc(PyObject *self)
{
    PyTypeObject *tp = Py_TYPE(self);
    tp->tp_free(self);
    Py_DECREF(tp);
}

static PyType_Slot MyObj_slots[] = {
    {Py_tp_dealloc, MyObj_dealloc},
    {0, NULL}
};

static PyObject *
register_type(PyObject *self, PyObject *args)
{
    /* This passes MyObj_dealloc as a slot but should not flag it */
    Py_RETURN_NONE;
}
"""


class TestScanGilUsage(unittest.TestCase):
    """Test GIL discipline analysis."""

    def test_mismatched_allow_threads(self):
        """Detect mismatched BEGIN/END_ALLOW_THREADS."""
        with TempExtension({"bad.c": MISMATCHED_GIL}) as root:
            result = gil.analyze(str(root / "bad.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("mismatched_allow_threads", types)

    def test_api_without_gil(self):
        """Detect Python API call in GIL-released region."""
        with TempExtension({"api.c": API_WITHOUT_GIL}) as root:
            result = gil.analyze(str(root / "api.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("api_without_gil", types)

    def test_blocking_with_gil(self):
        """Detect blocking call with GIL held."""
        with TempExtension({"block.c": BLOCKING_WITH_GIL}) as root:
            result = gil.analyze(str(root / "block.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("blocking_with_gil", types)

    def test_free_threading_concern(self):
        """Detect static mutable PyObject* as free-threading concern."""
        with TempExtension({"global.c": GLOBAL_PYOBJECT}) as root:
            result = gil.analyze(str(root / "global.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("free_threading_concern", types)

    def test_callback_without_gil(self):
        """Detect callback function calling Python APIs without GIL."""
        with TempExtension({"cb.c": CALLBACK_WITHOUT_GIL}) as root:
            result = gil.analyze(str(root / "cb.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("callback_without_gil", types)
            cb_findings = [f for f in result["findings"]
                           if f["type"] == "callback_without_gil"]
            self.assertEqual(cb_findings[0]["function"], "my_callback")

    def test_type_slot_dealloc_not_flagged(self):
        """Functions assigned to tp_dealloc are not flagged as callbacks."""
        with TempExtension({"typed.c": TYPE_SLOT_NOT_FLAGGED}) as root:
            result = gil.analyze(str(root / "typed.c"))
            cb_findings = [f for f in result["findings"]
                           if f["type"] == "callback_without_gil"]
            flagged_names = [f["function"] for f in cb_findings]
            self.assertNotIn("MyObj_dealloc", flagged_names)
            self.assertNotIn("MyObj_traverse", flagged_names)

    def test_type_spec_slot_not_flagged(self):
        """Functions in PyType_Slot arrays are not flagged as callbacks."""
        with TempExtension({"spec.c": TYPE_SLOT_SPEC_NOT_FLAGGED}) as root:
            result = gil.analyze(str(root / "spec.c"))
            cb_findings = [f for f in result["findings"]
                           if f["type"] == "callback_without_gil"]
            flagged_names = [f["function"] for f in cb_findings]
            self.assertNotIn("MyObj_dealloc", flagged_names)

    def test_real_callback_still_flagged(self):
        """Non-type-slot callbacks are still flagged."""
        with TempExtension({"cb.c": CALLBACK_WITHOUT_GIL}) as root:
            result = gil.analyze(str(root / "cb.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("callback_without_gil", types)

    def test_minimal_extension_runs(self):
        """Script runs without error on minimal extension."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = gil.analyze(str(root / "myext.c"))
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            # Envelope sanity: data files loaded + at least one function seen.
            self.assertIn("functions_analyzed", result)
            self.assertGreaterEqual(result["functions_analyzed"], 1)


if __name__ == "__main__":
    unittest.main()
