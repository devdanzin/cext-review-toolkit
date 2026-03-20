"""Tests for scan_type_slots.py — type definition analysis."""

import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_TYPE

type_slots = import_script("scan_type_slots")


BUGGY_TYPE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
    PyObject *callback;
} BuggyObj;

static void
BuggyObj_dealloc(BuggyObj *self)
{
    /* BUG: missing PyObject_GC_UnTrack */
    Py_XDECREF(self->data);
    /* BUG: missing XDECREF for callback */
    PyObject_Del(self);  /* BUG: should use tp_free */
}

static int
BuggyObj_traverse(BuggyObj *self, visitproc visit, void *arg)
{
    Py_VISIT(self->data);
    /* BUG: missing Py_VISIT(self->callback) */
    return 0;
}

static PyTypeObject BuggyObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "buggy.BuggyObj",
    .tp_basicsize = sizeof(BuggyObj),
    .tp_dealloc = (destructor)BuggyObj_dealloc,
    .tp_traverse = (traverseproc)BuggyObj_traverse,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
};
"""

CORRECT_TYPE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} GoodObj;

static void
GoodObj_dealloc(GoodObj *self)
{
    PyObject_GC_UnTrack(self);
    Py_XDECREF(self->data);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
GoodObj_traverse(GoodObj *self, visitproc visit, void *arg)
{
    Py_VISIT(self->data);
    return 0;
}

static PyTypeObject GoodObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "good.GoodObj",
    .tp_basicsize = sizeof(GoodObj),
    .tp_dealloc = (destructor)GoodObj_dealloc,
    .tp_traverse = (traverseproc)GoodObj_traverse,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
};
"""

RICHCOMPARE_BUG = """\
#include <Python.h>

static PyObject *
MyObj_richcompare(PyObject *self, PyObject *other, int op)
{
    if (op != Py_EQ && op != Py_NE)
        return Py_NotImplemented;  /* BUG: missing Py_INCREF */
    Py_RETURN_TRUE;
}

static PyTypeObject MyObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mymod.MyObj",
    .tp_basicsize = sizeof(PyObject),
    .tp_richcompare = MyObj_richcompare,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""


def test_dealloc_missing_tp_free():
    """Detect dealloc using PyObject_Del instead of tp_free."""
    with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
        result = type_slots.analyze(str(root / "buggy.c"))
        types = [f["type"] for f in result["findings"]]
        assert "dealloc_wrong_free" in types


def test_dealloc_missing_untrack():
    """Detect dealloc missing PyObject_GC_UnTrack."""
    with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
        result = type_slots.analyze(str(root / "buggy.c"))
        types = [f["type"] for f in result["findings"]]
        assert "dealloc_missing_untrack" in types


def test_traverse_missing_member():
    """Detect traverse not visiting all PyObject* members."""
    with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
        result = type_slots.analyze(str(root / "buggy.c"))
        missing = [f for f in result["findings"]
                   if f["type"] == "traverse_missing_member"]
        assert len(missing) >= 1
        members = [f["missing_member"] for f in missing]
        assert "callback" in members


def test_correct_type_minimal_findings():
    """Correct type should have no dealloc or traverse findings."""
    with TempExtension({"good.c": CORRECT_TYPE}) as root:
        result = type_slots.analyze(str(root / "good.c"))
        bad = [f for f in result["findings"]
               if f["type"] in ("dealloc_missing_tp_free", "dealloc_wrong_free",
                                 "dealloc_missing_untrack", "traverse_missing_member")]
        assert len(bad) == 0


def test_richcompare_not_incref():
    """Detect missing Py_INCREF(Py_NotImplemented)."""
    with TempExtension({"rich.c": RICHCOMPARE_BUG}) as root:
        result = type_slots.analyze(str(root / "rich.c"))
        types = [f["type"] for f in result["findings"]]
        assert "richcompare_not_incref_notimplemented" in types


def test_extension_with_type_runs():
    """Script runs on EXTENSION_WITH_TYPE fixture."""
    with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
        result = type_slots.analyze(str(root / "typed.c"))
        assert "findings" in result
        assert "summary" in result
