"""Tests for scan_type_slots.py — type definition analysis."""

import unittest
from helpers import import_script, TempExtension, EXTENSION_WITH_TYPE

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


class TestScanTypeSlots(unittest.TestCase):
    """Test type definition correctness analysis."""

    def test_dealloc_missing_tp_free(self):
        """Detect dealloc using PyObject_Del instead of tp_free."""
        with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
            result = type_slots.analyze(str(root / "buggy.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("dealloc_wrong_free", types)

    def test_dealloc_missing_untrack(self):
        """Detect dealloc missing PyObject_GC_UnTrack."""
        with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
            result = type_slots.analyze(str(root / "buggy.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("dealloc_missing_untrack", types)

    def test_traverse_missing_member(self):
        """Detect traverse not visiting all PyObject* members."""
        with TempExtension({"buggy.c": BUGGY_TYPE}) as root:
            result = type_slots.analyze(str(root / "buggy.c"))
            missing = [
                f for f in result["findings"] if f["type"] == "traverse_missing_member"
            ]
            self.assertGreaterEqual(len(missing), 1)
            members = [f["missing_member"] for f in missing]
            self.assertIn("callback", members)

    def test_correct_type_minimal_findings(self):
        """Correct type should have no dealloc or traverse findings."""
        with TempExtension({"good.c": CORRECT_TYPE}) as root:
            result = type_slots.analyze(str(root / "good.c"))
            bad = [
                f
                for f in result["findings"]
                if f["type"]
                in (
                    "dealloc_missing_tp_free",
                    "dealloc_wrong_free",
                    "dealloc_missing_untrack",
                    "traverse_missing_member",
                )
            ]
            self.assertEqual(len(bad), 0)

    def test_richcompare_not_incref(self):
        """Detect missing Py_INCREF(Py_NotImplemented)."""
        with TempExtension({"rich.c": RICHCOMPARE_BUG}) as root:
            result = type_slots.analyze(str(root / "rich.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("richcompare_not_incref_notimplemented", types)

    def test_extension_with_type_runs(self):
        """Script runs on EXTENSION_WITH_TYPE fixture."""
        with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
            result = type_slots.analyze(str(root / "typed.c"))
            self.assertIn("findings", result)
            self.assertIn("summary", result)


TYPE_SPEC_SENTINEL_ZERO = """\
#include <Python.h>

static PyObject *dummy(PyObject *self, PyObject *args) { Py_RETURN_NONE; }

static PyType_Slot MyType_slots[] = {
    {Py_tp_new, PyType_GenericNew},
    {0, 0}
};

static PyType_Spec MyType_spec = {
    .name = "mymod.MyType",
    .basicsize = sizeof(PyObject),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = MyType_slots,
};
"""

TYPE_SPEC_SENTINEL_NULL = """\
#include <Python.h>

static PyObject *dummy(PyObject *self, PyObject *args) { Py_RETURN_NONE; }

static PyType_Slot MyType_slots[] = {
    {Py_tp_new, PyType_GenericNew},
    {0, NULL}
};

static PyType_Spec MyType_spec = {
    .name = "mymod.MyType",
    .basicsize = sizeof(PyObject),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = MyType_slots,
};
"""

TYPE_SPEC_MISSING_SENTINEL = """\
#include <Python.h>

static PyObject *dummy(PyObject *self, PyObject *args) { Py_RETURN_NONE; }

static PyType_Slot MyType_slots[] = {
    {Py_tp_new, PyType_GenericNew},
};

static PyType_Spec MyType_spec = {
    .name = "mymod.MyType",
    .basicsize = sizeof(PyObject),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = MyType_slots,
};
"""


class TestTypeSpecSentinel(unittest.TestCase):
    """Test PyType_Slot sentinel detection."""

    def test_sentinel_zero_zero_accepted(self):
        """{0, 0} sentinel is accepted (no false positive)."""
        with TempExtension({"ext.c": TYPE_SPEC_SENTINEL_ZERO}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            sentinel_findings = [
                f
                for f in result["findings"]
                if f["type"] == "type_spec_missing_sentinel"
            ]
            self.assertEqual(len(sentinel_findings), 0)

    def test_sentinel_zero_null_accepted(self):
        """{0, NULL} sentinel is accepted."""
        with TempExtension({"ext.c": TYPE_SPEC_SENTINEL_NULL}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            sentinel_findings = [
                f
                for f in result["findings"]
                if f["type"] == "type_spec_missing_sentinel"
            ]
            self.assertEqual(len(sentinel_findings), 0)

    def test_missing_sentinel_detected(self):
        """Missing sentinel is flagged."""
        with TempExtension({"ext.c": TYPE_SPEC_MISSING_SENTINEL}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            sentinel_findings = [
                f
                for f in result["findings"]
                if f["type"] == "type_spec_missing_sentinel"
            ]
            self.assertGreater(len(sentinel_findings), 0)


DEALLOC_INCOMPLETE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *name;
    PyObject *value;
    PyObject *extra;
} IncompleteObj;

static void
IncompleteObj_dealloc(IncompleteObj *self)
{
    Py_XDECREF(self->name);
    Py_XDECREF(self->value);
    /* BUG: self->extra not cleaned up */
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyTypeObject IncompleteObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.IncompleteObj",
    .tp_basicsize = sizeof(IncompleteObj),
    .tp_dealloc = (destructor)IncompleteObj_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

DEALLOC_COMPLETE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *name;
    PyObject *value;
} CompleteObj;

static void
CompleteObj_dealloc(CompleteObj *self)
{
    Py_XDECREF(self->name);
    Py_XDECREF(self->value);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyTypeObject CompleteObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.CompleteObj",
    .tp_basicsize = sizeof(CompleteObj),
    .tp_dealloc = (destructor)CompleteObj_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""


class TestDeallocCompleteness(unittest.TestCase):
    """Test dealloc completeness checking."""

    def test_detects_missing_xdecref_in_dealloc(self):
        """Detect PyObject* member not XDECREF'd in dealloc."""
        with TempExtension({"ext.c": DEALLOC_INCOMPLETE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            missing = [
                f for f in result["findings"] if f["type"] == "dealloc_missing_xdecref"
            ]
            self.assertGreater(len(missing), 0)
            names = [f["missing_member"] for f in missing]
            self.assertIn("extra", names)
            self.assertNotIn("name", names)
            self.assertNotIn("value", names)

    def test_complete_dealloc_no_finding(self):
        """Complete dealloc produces no dealloc_missing_xdecref findings."""
        with TempExtension({"ext.c": DEALLOC_COMPLETE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            missing = [
                f for f in result["findings"] if f["type"] == "dealloc_missing_xdecref"
            ]
            self.assertEqual(len(missing), 0)


INIT_REINIT_UNSAFE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
    char *buffer;
} UnsafeObj;

static int
UnsafeObj_init(UnsafeObj *self, PyObject *args, PyObject *kwds)
{
    /* BUG: allocates without checking if already initialized */
    self->data = PyList_New(0);
    self->buffer = PyMem_Malloc(1024);
    return 0;
}

static PyTypeObject UnsafeObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.UnsafeObj",
    .tp_basicsize = sizeof(UnsafeObj),
    .tp_init = (initproc)UnsafeObj_init,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

INIT_REINIT_SAFE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} SafeObj;

static int
SafeObj_init(SafeObj *self, PyObject *args, PyObject *kwds)
{
    /* Safe: cleans up before re-init */
    if (self->data != NULL) {
        Py_CLEAR(self->data);
    }
    self->data = PyList_New(0);
    return 0;
}

static PyTypeObject SafeObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.SafeObj",
    .tp_basicsize = sizeof(SafeObj),
    .tp_init = (initproc)SafeObj_init,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

NEW_WITHOUT_INIT_UNSAFE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
    char *buffer;
} BadNewObj;

static PyObject *
BadNewObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    /* BUG: uses PyObject_New (non-zeroing) without initializing members */
    BadNewObj *self = (BadNewObj *)PyObject_New(BadNewObj, type);
    /* data and buffer are uninitialized garbage */
    return (PyObject *)self;
}

static PyTypeObject BadNewObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.BadNewObj",
    .tp_basicsize = sizeof(BadNewObj),
    .tp_new = BadNewObj_new,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

NEW_WITH_INIT_SAFE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} GoodNewObj;

static PyObject *
GoodNewObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    /* Safe: uses tp_alloc which zeros memory */
    GoodNewObj *self = (GoodNewObj *)type->tp_alloc(type, 0);
    return (PyObject *)self;
}

static PyTypeObject GoodNewObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.GoodNewObj",
    .tp_basicsize = sizeof(GoodNewObj),
    .tp_new = GoodNewObj_new,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""


class TestInitReinitSafety(unittest.TestCase):
    """Test tp_init re-init safety detection."""

    def test_detects_unsafe_reinit(self):
        """Detect tp_init that allocates without re-init guard."""
        with TempExtension({"ext.c": INIT_REINIT_UNSAFE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("init_not_reinit_safe", types)

    def test_safe_reinit_no_finding(self):
        """tp_init with re-init guard produces no finding."""
        with TempExtension({"ext.c": INIT_REINIT_SAFE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            reinit = [
                f for f in result["findings"] if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)


class TestNewWithoutInit(unittest.TestCase):
    """Test tp_new without tp_init safety detection."""

    def test_detects_uninitialized_members(self):
        """Detect tp_new with non-zeroing alloc and uninitialized members."""
        with TempExtension({"ext.c": NEW_WITHOUT_INIT_UNSAFE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("new_missing_member_init", types)

    def test_zeroing_alloc_no_finding(self):
        """tp_new using tp_alloc (zeroing) produces no finding."""
        with TempExtension({"ext.c": NEW_WITH_INIT_SAFE}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            new_findings = [
                f for f in result["findings"] if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(new_findings), 0)


TYPE_WITH_NEW_AND_INIT = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} BothObj;

static PyObject *
BothObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    BothObj *self = (BothObj *)type->tp_alloc(type, 0);
    return (PyObject *)self;
}

static int
BothObj_init(BothObj *self, PyObject *args, PyObject *kwds)
{
    self->data = PyList_New(0);
    return 0;
}

static PyTypeObject BothObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.BothObj",
    .tp_basicsize = sizeof(BothObj),
    .tp_new = BothObj_new,
    .tp_init = (initproc)BothObj_init,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

TYPE_WITH_ONLY_NEW = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} OnlyNewObj;

static PyObject *
OnlyNewObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    OnlyNewObj *self = (OnlyNewObj *)type->tp_alloc(type, 0);
    if (self != NULL) {
        self->data = PyList_New(0);
    }
    return (PyObject *)self;
}

static PyTypeObject OnlyNewObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.OnlyNewObj",
    .tp_basicsize = sizeof(OnlyNewObj),
    .tp_new = OnlyNewObj_new,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""

TYPE_WITH_GENERIC_NEW_AND_INIT = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} GenericNewObj;

static int
GenericNewObj_init(GenericNewObj *self, PyObject *args, PyObject *kwds)
{
    self->data = PyList_New(0);
    return 0;
}

static PyTypeObject GenericNewObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "test.GenericNewObj",
    .tp_basicsize = sizeof(GenericNewObj),
    .tp_new = PyType_GenericNew,
    .tp_init = (initproc)GenericNewObj_init,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
"""


class TestNewAndInitPartialState(unittest.TestCase):
    """Test new_and_init_partial_state triage check."""

    def test_detects_both_new_and_init(self):
        """Flag type with both custom tp_new and tp_init."""
        with TempExtension({"ext.c": TYPE_WITH_NEW_AND_INIT}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("new_and_init_partial_state", types)

    def test_only_new_no_finding(self):
        """Type with only tp_new (no tp_init) produces no finding."""
        with TempExtension({"ext.c": TYPE_WITH_ONLY_NEW}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            partial = [
                f
                for f in result["findings"]
                if f["type"] == "new_and_init_partial_state"
            ]
            self.assertEqual(len(partial), 0)

    def test_generic_new_with_init_no_finding(self):
        """Type with PyType_GenericNew + tp_init produces no finding."""
        with TempExtension({"ext.c": TYPE_WITH_GENERIC_NEW_AND_INIT}) as root:
            result = type_slots.analyze(str(root / "ext.c"))
            partial = [
                f
                for f in result["findings"]
                if f["type"] == "new_and_init_partial_state"
            ]
            self.assertEqual(len(partial), 0)


if __name__ == "__main__":
    unittest.main()
