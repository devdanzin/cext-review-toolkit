"""Tests for scan_module_state.py — module init and state analysis."""

import pytest
from helpers import (
    import_script, TempExtension,
    MINIMAL_EXTENSION, MULTI_PHASE_EXTENSION,
    EXTENSION_WITH_TYPE,
)

module_state = import_script("scan_module_state")


GLOBAL_STATE_EXT = """\
#include <Python.h>

static PyObject *global_cache = NULL;
static int initialized = 0;

static PyMethodDef methods[] = {
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "stateful",
    NULL,
    -1,
    methods
};

PyMODINIT_FUNC
PyInit_stateful(void)
{
    return PyModule_Create(&module);
}
"""


def test_single_phase_init_detected():
    """MINIMAL_EXTENSION uses single-phase init — should be detected."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = module_state.analyze(str(root / "myext.c"))
        types = [f["type"] for f in result["findings"]]
        assert "single_phase_init" in types


def test_multi_phase_init_not_flagged():
    """MULTI_PHASE_EXTENSION should NOT trigger single_phase_init."""
    with TempExtension({"myext.c": MULTI_PHASE_EXTENSION}) as root:
        result = module_state.analyze(str(root / "myext.c"))
        single = [f for f in result["findings"]
                  if f["type"] == "single_phase_init"]
        assert len(single) == 0


def test_global_pyobject_state():
    """Detect static PyObject* global state."""
    with TempExtension({"state.c": GLOBAL_STATE_EXT}) as root:
        result = module_state.analyze(str(root / "state.c"))
        types = [f["type"] for f in result["findings"]]
        assert "global_pyobject_state" in types
        pyobj = [f for f in result["findings"]
                 if f["type"] == "global_pyobject_state"]
        vars_found = [f["variable"] for f in pyobj]
        assert "global_cache" in vars_found


def test_static_mutable_state():
    """Detect non-PyObject static mutable state."""
    with TempExtension({"state.c": GLOBAL_STATE_EXT}) as root:
        result = module_state.analyze(str(root / "state.c"))
        types = [f["type"] for f in result["findings"]]
        assert "static_mutable_state" in types


def test_static_type_object():
    """Detect static PyTypeObject in EXTENSION_WITH_TYPE."""
    with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
        result = module_state.analyze(str(root / "typed.c"))
        types = [f["type"] for f in result["findings"]]
        assert "static_type_object" in types


def test_module_add_object_misuse():
    """Detect PyModule_AddObject usage in EXTENSION_WITH_TYPE."""
    with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
        result = module_state.analyze(str(root / "typed.c"))
        types = [f["type"] for f in result["findings"]]
        assert "module_add_object_misuse" in types


def test_output_structure():
    """Output has correct envelope."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = module_state.analyze(str(root / "myext.c"))
        assert "project_root" in result
        assert "findings" in result
        assert "summary" in result
        assert "by_type" in result["summary"]
