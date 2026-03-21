"""Tests for scan_module_state.py — module init and state analysis."""

import unittest
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


class TestScanModuleState(unittest.TestCase):
    """Test module state management analysis."""

    def test_single_phase_init_detected(self):
        """MINIMAL_EXTENSION uses single-phase init -- should be detected."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = module_state.analyze(str(root / "myext.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("single_phase_init", types)

    def test_multi_phase_init_not_flagged(self):
        """MULTI_PHASE_EXTENSION should NOT trigger single_phase_init."""
        with TempExtension({"myext.c": MULTI_PHASE_EXTENSION}) as root:
            result = module_state.analyze(str(root / "myext.c"))
            single = [f for f in result["findings"]
                      if f["type"] == "single_phase_init"]
            self.assertEqual(len(single), 0)

    def test_global_pyobject_state(self):
        """Detect static PyObject* global state."""
        with TempExtension({"state.c": GLOBAL_STATE_EXT}) as root:
            result = module_state.analyze(str(root / "state.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("global_pyobject_state", types)
            pyobj = [f for f in result["findings"]
                     if f["type"] == "global_pyobject_state"]
            vars_found = [f["variable"] for f in pyobj]
            self.assertIn("global_cache", vars_found)

    def test_static_mutable_state(self):
        """Detect non-PyObject static mutable state."""
        with TempExtension({"state.c": GLOBAL_STATE_EXT}) as root:
            result = module_state.analyze(str(root / "state.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("static_mutable_state", types)

    def test_static_type_object(self):
        """Detect static PyTypeObject in EXTENSION_WITH_TYPE."""
        with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
            result = module_state.analyze(str(root / "typed.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("static_type_object", types)

    def test_module_add_object_misuse(self):
        """Detect PyModule_AddObject usage in EXTENSION_WITH_TYPE."""
        with TempExtension({"typed.c": EXTENSION_WITH_TYPE}) as root:
            result = module_state.analyze(str(root / "typed.c"))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("module_add_object_misuse", types)

    def test_output_structure(self):
        """Output has correct envelope."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = module_state.analyze(str(root / "myext.c"))
            self.assertIn("project_root", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("by_type", result["summary"])


if __name__ == "__main__":
    unittest.main()
