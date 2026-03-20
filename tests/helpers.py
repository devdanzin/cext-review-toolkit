"""Test helpers for cext-review-toolkit tests."""

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path


def import_script(name: str):
    """Import a script from plugins/cext-review-toolkit/scripts/ as a module."""
    script_dir = Path(__file__).resolve().parent.parent / "plugins" / "cext-review-toolkit" / "scripts"
    spec = importlib.util.spec_from_file_location(name, script_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TempExtension:
    """Create a temporary C extension project for testing.

    Usage:
        with TempExtension({"src/myext.c": c_code}) as root:
            result = some_script.analyze(str(root))

    Options:
        files: dict mapping relative paths to content
        setup_py: optional setup.py content (auto-generated if source files present)
        pyproject_toml: optional pyproject.toml content
        init_git: whether to initialize a git repo (default: False)
    """

    def __init__(self, files: dict[str, str], *,
                 setup_py: str | None = None,
                 pyproject_toml: str | None = None,
                 init_git: bool = False):
        self.files = files
        self.setup_py = setup_py
        self.pyproject_toml = pyproject_toml
        self.init_git = init_git
        self._tmpdir = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="cext_test_")
        root = Path(self._tmpdir)

        for rel_path, content in self.files.items():
            full_path = root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        if self.setup_py:
            (root / "setup.py").write_text(self.setup_py, encoding="utf-8")

        if self.pyproject_toml:
            (root / "pyproject.toml").write_text(self.pyproject_toml, encoding="utf-8")

        if self.init_git:
            import subprocess
            subprocess.run(["git", "init"], cwd=str(root), capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=str(root), capture_output=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                     "GIT_AUTHOR_EMAIL": "test@test.com",
                     "GIT_COMMITTER_NAME": "Test",
                     "GIT_COMMITTER_EMAIL": "test@test.com"},
            )

        return root

    def __exit__(self, *args):
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# Common C code fixtures for testing

MINIMAL_EXTENSION = """\
#include <Python.h>

static PyObject *
myext_hello(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}

static PyMethodDef myext_methods[] = {
    {"hello", myext_hello, METH_NOARGS, "Say hello."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef myext_module = {
    PyModuleDef_HEAD_INIT,
    "myext",
    NULL,
    -1,
    myext_methods
};

PyMODINIT_FUNC
PyInit_myext(void)
{
    return PyModule_Create(&myext_module);
}
"""

MULTI_PHASE_EXTENSION = """\
#include <Python.h>

static PyObject *
myext_hello(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}

static PyMethodDef myext_methods[] = {
    {"hello", myext_hello, METH_NOARGS, "Say hello."},
    {NULL, NULL, 0, NULL}
};

static int
myext_exec(PyObject *module)
{
    return 0;
}

static PyModuleDef_Slot myext_slots[] = {
    {Py_mod_exec, myext_exec},
    {0, NULL}
};

static struct PyModuleDef myext_module = {
    PyModuleDef_HEAD_INIT,
    "myext",
    NULL,
    0,
    myext_methods,
    myext_slots,
};

PyMODINIT_FUNC
PyInit_myext(void)
{
    return PyModuleDef_Init(&myext_module);
}
"""

EXTENSION_WITH_TYPE = """\
#include <Python.h>
#include "structmember.h"

typedef struct {
    PyObject_HEAD
    PyObject *name;
    PyObject *value;
    int count;
} MyObj;

static void
MyObj_dealloc(MyObj *self)
{
    Py_XDECREF(self->name);
    Py_XDECREF(self->value);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
MyObj_traverse(MyObj *self, visitproc visit, void *arg)
{
    Py_VISIT(self->name);
    Py_VISIT(self->value);
    return 0;
}

static PyObject *
MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    MyObj *self;
    self = (MyObj *)type->tp_alloc(type, 0);
    if (self != NULL) {
        self->name = PyUnicode_FromString("");
        if (self->name == NULL) {
            Py_DECREF(self);
            return NULL;
        }
        self->value = Py_None;
        Py_INCREF(Py_None);
        self->count = 0;
    }
    return (PyObject *)self;
}

static PyMemberDef MyObj_members[] = {
    {"count", T_INT, offsetof(MyObj, count), 0, "count"},
    {NULL}
};

static PyTypeObject MyObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "myext.MyObj",
    .tp_basicsize = sizeof(MyObj),
    .tp_dealloc = (destructor)MyObj_dealloc,
    .tp_traverse = (traverseproc)MyObj_traverse,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_new = MyObj_new,
    .tp_members = MyObj_members,
};

static struct PyModuleDef myext_module = {
    PyModuleDef_HEAD_INIT,
    "myext",
    NULL,
    -1,
    NULL
};

PyMODINIT_FUNC
PyInit_myext(void)
{
    PyObject *m;
    if (PyType_Ready(&MyObjType) < 0)
        return NULL;
    m = PyModule_Create(&myext_module);
    if (m == NULL)
        return NULL;
    Py_INCREF(&MyObjType);
    if (PyModule_AddObject(m, "MyObj", (PyObject *)&MyObjType) < 0) {
        Py_DECREF(&MyObjType);
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
"""

EXTENSION_WITH_BUGS = """\
#include <Python.h>

/* Global mutable state — should be in module state */
static PyObject *global_cache = NULL;
static int initialized = 0;

static PyObject *
leaky_function(PyObject *self, PyObject *args)
{
    PyObject *result = PyList_New(0);
    if (result == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    /* BUG: if Append fails, item is leaked */
    if (PyList_Append(result, item) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    Py_DECREF(item);
    return result;
}

static PyObject *
borrowed_ref_bug(PyObject *self, PyObject *args)
{
    PyObject *list;
    if (!PyArg_ParseTuple(args, "O", &list))
        return NULL;

    /* BUG: borrowed ref from GET_ITEM, then callback into Python */
    PyObject *item = PyList_GET_ITEM(list, 0);
    PyObject *str_item = PyObject_Str(item);  /* This could invalidate item! */
    if (str_item == NULL)
        return NULL;

    /* Using item after it may have been invalidated */
    PyObject *result = PyObject_RichCompare(item, str_item, Py_EQ);
    Py_DECREF(str_item);
    return result;
}

static PyObject *
null_deref_bug(PyObject *self, PyObject *args)
{
    /* BUG: no NULL check before use */
    PyObject *obj = PyDict_GetItemString(global_cache, "key");
    return PyObject_Str(obj);  /* obj could be NULL */
}

static PyMethodDef methods[] = {
    {"leaky", leaky_function, METH_NOARGS, NULL},
    {"borrowed", borrowed_ref_bug, METH_VARARGS, NULL},
    {"null_deref", null_deref_bug, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "buggy",
    NULL,
    -1,
    methods
};

PyMODINIT_FUNC
PyInit_buggy(void)
{
    return PyModule_Create(&module);
}
"""

SETUP_PY_TEMPLATE = """\
from setuptools import setup, Extension

setup(
    name="{name}",
    ext_modules=[
        Extension("{name}", sources={sources}),
    ],
    python_requires="{python_requires}",
)
"""
