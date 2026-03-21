"""Tests for discover_extension.py — C extension project discovery."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, SETUP_PY_TEMPLATE

discover = import_script("discover_extension")


class TestDiscoverExtension(unittest.TestCase):
    """Test C extension project discovery."""

    def test_detect_setup_py(self):
        """Extension detected from setup.py."""
        setup_py = SETUP_PY_TEMPLATE.format(
            name="myext",
            sources='["src/myext.c"]',
            python_requires=">=3.9",
        )
        with TempExtension({"src/myext.c": MINIMAL_EXTENSION}, setup_py=setup_py) as root:
            result = discover.discover(str(root))
            self.assertGreaterEqual(len(result["extensions"]), 1)
            ext = result["extensions"][0]
            self.assertEqual(ext["module_name"], "myext")
            self.assertEqual(ext["detection_method"], "setup_py")
            self.assertIn("src/myext.c", ext["source_files"])

    def test_detect_python_h_include(self):
        """Fallback detection from #include <Python.h>."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = discover.discover(str(root))
            self.assertGreaterEqual(len(result["extensions"]), 1)
            ext = result["extensions"][0]
            self.assertEqual(ext["detection_method"], "python_h_include")

    def test_detect_init_function(self):
        """PyInit_* function found."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = discover.discover(str(root))
            self.assertGreaterEqual(len(result["init_functions"]), 1)
            init_names = list(result["init_functions"].values())
            self.assertTrue(any("PyInit_" in name for name in init_names))

    def test_detect_limited_api(self):
        """Py_LIMITED_API define detected."""
        code = '#define Py_LIMITED_API 0x030A0000\n' + MINIMAL_EXTENSION
        with TempExtension({"myext.c": code}) as root:
            result = discover.discover(str(root))
            self.assertTrue(result["limited_api"])
            self.assertEqual(result["limited_api_version"], "0x030A0000")

    def test_no_extension_found(self):
        """Directory with no C extension returns empty list."""
        with TempExtension({"readme.txt": "just a text file"}) as root:
            result = discover.discover(str(root))
            self.assertEqual(len(result["extensions"]), 0)

    def test_multiple_extensions(self):
        """setup.py with multiple ext_modules."""
        setup_py = """\
from setuptools import setup, Extension

setup(
    name="multi",
    ext_modules=[
        Extension("ext_a", sources=["a.c"]),
        Extension("ext_b", sources=["b.c", "b_util.c"]),
    ],
)
"""
        code_a = '#include <Python.h>\nPyMODINIT_FUNC PyInit_ext_a(void) { return NULL; }\n'
        code_b = '#include <Python.h>\nPyMODINIT_FUNC PyInit_ext_b(void) { return NULL; }\n'
        with TempExtension(
            {"a.c": code_a, "b.c": code_b, "b_util.c": '#include <Python.h>\n'},
            setup_py=setup_py,
        ) as root:
            result = discover.discover(str(root))
            self.assertEqual(len(result["extensions"]), 2)
            names = [e["module_name"] for e in result["extensions"]]
            self.assertIn("ext_a", names)
            self.assertIn("ext_b", names)

    def test_pyproject_toml(self):
        """Detection from pyproject.toml with setuptools ext-modules."""
        pyproject = """\
[build-system]
requires = ["setuptools"]

[[tool.setuptools.ext-modules]]
name = "myext"
sources = ["myext.c"]
"""
        with TempExtension(
            {"myext.c": MINIMAL_EXTENSION},
            pyproject_toml=pyproject,
        ) as root:
            result = discover.discover(str(root))
            self.assertGreaterEqual(len(result["extensions"]), 1)
            ext = result["extensions"][0]
            self.assertEqual(ext["module_name"], "myext")
            self.assertEqual(ext["detection_method"], "pyproject_toml")

    def test_meson_build(self):
        """Detection from meson.build."""
        meson_content = """\
project('myext', 'c')
py = import('python').find_installation()
py.extension_module('myext', ['myext.c', 'util.c'])
"""
        files = {
            "myext.c": MINIMAL_EXTENSION,
            "util.c": '#include <Python.h>\n',
            "meson.build": meson_content,
        }
        with TempExtension(files) as root:
            result = discover.discover(str(root))
            self.assertGreaterEqual(len(result["extensions"]), 1)
            ext = result["extensions"][0]
            self.assertEqual(ext["module_name"], "myext")
            self.assertEqual(ext["detection_method"], "meson_build")

    def test_python_requires_from_setup_py(self):
        """python_requires extracted from setup.py."""
        setup_py = SETUP_PY_TEMPLATE.format(
            name="myext",
            sources='["myext.c"]',
            python_requires=">=3.9",
        )
        with TempExtension({"myext.c": MINIMAL_EXTENSION}, setup_py=setup_py) as root:
            result = discover.discover(str(root))
            self.assertEqual(result["python_requires"], ">=3.9")

    def test_total_c_files_count(self):
        """total_c_files counts all .c files found."""
        with TempExtension({
            "a.c": '#include <Python.h>\n',
            "b.c": '#include <Python.h>\n',
            "lib/c.c": '#include <Python.h>\n',
        }) as root:
            result = discover.discover(str(root))
            self.assertEqual(result["total_c_files"], 3)


if __name__ == "__main__":
    unittest.main()
