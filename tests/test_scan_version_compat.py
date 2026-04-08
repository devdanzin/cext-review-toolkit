"""Tests for scan_version_compat.py — version compatibility scanning."""

import unittest
from helpers import import_script, TempExtension

mod = import_script("scan_version_compat")


REMOVED_API = """\
#include <Python.h>

static PyObject *
call_removed(PyObject *self, PyObject *args)
{
    return PyCFunction_Call(self, args, NULL);
}
"""

DEPRECATED_API = """\
#include <Python.h>

static int
use_deprecated(PyObject *mod)
{
    PyModule_AddObject(mod, "x", Py_None);
    return 0;
}
"""

GUARDED_API = """\
#include <Python.h>

static PyObject *
use_guarded(PyObject *self, PyObject *args)
{
#if PY_VERSION_HEX >= 0x030A0000
    return Py_NewRef(self);
#else
    Py_INCREF(self);
    return self;
#endif
}
"""

DEAD_GUARD = """\
#include <Python.h>

#if PY_VERSION_HEX < 0x03090000
/* Dead code if min python is 3.9 */
static void old_compat(void) {}
#endif

static PyObject *
current_func(PyObject *self, PyObject *args)
{
    Py_RETURN_NONE;
}
"""


class TestScanVersionCompat(unittest.TestCase):
    """Test version compatibility scanning."""

    def test_detects_removed_api(self):
        """Detect usage of removed API."""
        with TempExtension({"ext.c": REMOVED_API}) as root:
            result = mod.analyze(str(root), min_python="3.15")
            types = [f["type"] for f in result["findings"]]
            self.assertIn("removed_api_usage", types)

    def test_detects_deprecated_api(self):
        """Detect usage of deprecated API."""
        with TempExtension({"ext.c": DEPRECATED_API}) as root:
            result = mod.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("deprecated_api_usage", types)

    def test_guarded_api_no_finding(self):
        """Version-guarded API usage is not flagged as removed."""
        with TempExtension({"ext.c": GUARDED_API}) as root:
            result = mod.analyze(str(root))
            removed = [f for f in result["findings"]
                       if f["type"] == "removed_api_usage"]
            self.assertEqual(len(removed), 0)

    def test_dead_version_guard(self):
        """Detect dead version guard below minimum Python."""
        with TempExtension({"ext.c": DEAD_GUARD}) as root:
            result = mod.analyze(str(root), min_python="3.9")
            types = [f["type"] for f in result["findings"]]
            self.assertIn("dead_version_guard", types)

    def test_output_envelope(self):
        """Output has correct structure."""
        with TempExtension({"ext.c": REMOVED_API}) as root:
            result = mod.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("min_python", result)


if __name__ == "__main__":
    unittest.main()
