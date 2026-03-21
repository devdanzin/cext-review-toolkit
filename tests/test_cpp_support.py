"""Tests for C++ file support in tree_sitter_utils and scan_common."""

import unittest
from pathlib import Path
from helpers import import_script, TempExtension

ts = import_script("tree_sitter_utils")
scan_common = import_script("scan_common")

CPP_EXTENSION = """\
#include <Python.h>

extern "C" {

static PyObject *
myext_hello(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello from C++");
}

static PyMethodDef methods[] = {
    {"hello", myext_hello, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "myext",
    NULL,
    -1,
    methods
};

PyMODINIT_FUNC
PyInit_myext(void)
{
    return PyModule_Create(&module);
}

}
"""


class TestCppSupport(unittest.TestCase):
    """Test C++ file parsing support."""

    def test_cpp_available_flag(self):
        """is_cpp_available returns bool."""
        result = ts.is_cpp_available()
        self.assertIsInstance(result, bool)

    def test_c_file_uses_c_parser(self):
        """C file still uses C parser."""
        parser = ts.get_parser_for_file(Path("test.c"))
        self.assertIs(parser, ts._parser)

    def test_h_file_uses_c_parser(self):
        """Header file uses C parser."""
        parser = ts.get_parser_for_file(Path("test.h"))
        self.assertIs(parser, ts._parser)

    def test_extension_constants(self):
        """Extension constants are defined correctly."""
        self.assertIn(".c", ts.C_EXTENSIONS)
        self.assertIn(".h", ts.C_EXTENSIONS)
        self.assertIn(".cpp", ts.CPP_EXTENSIONS)
        self.assertIn(".cc", ts.CPP_EXTENSIONS)
        self.assertIn(".cxx", ts.CPP_EXTENSIONS)
        self.assertTrue(ts.C_EXTENSIONS < ts.ALL_SOURCE_EXTENSIONS)

    @unittest.skipUnless(ts.is_cpp_available(), "tree-sitter-cpp not installed")
    def test_cpp_file_uses_cpp_parser(self):
        """C++ file uses C++ parser when available."""
        parser = ts.get_parser_for_file(Path("test.cpp"))
        self.assertIsNot(parser, ts._parser)

    @unittest.skipUnless(ts.is_cpp_available(), "tree-sitter-cpp not installed")
    def test_parse_cpp_file(self):
        """C++ file parsed without errors."""
        tree = ts.parse_bytes_for_file(
            CPP_EXTENSION.encode(), Path("test.cpp"))
        self.assertIsNotNone(tree)

    @unittest.skipUnless(ts.is_cpp_available(), "tree-sitter-cpp not installed")
    def test_extract_functions_from_cpp(self):
        """Functions extracted from C++ source."""
        source = CPP_EXTENSION.encode()
        tree = ts.parse_bytes_for_file(source, Path("test.cpp"))
        funcs = ts.extract_functions(tree, source)
        names = [f["name"] for f in funcs]
        self.assertIn("myext_hello", names)
        self.assertIn("PyInit_myext", names)

    @unittest.skipUnless(ts.is_cpp_available(), "tree-sitter-cpp not installed")
    def test_discover_c_files_includes_cpp(self):
        """discover_c_files finds .cpp files when cpp is available."""
        with TempExtension({
            "a.c": "#include <Python.h>\n",
            "b.cpp": "#include <Python.h>\n",
            "c.cxx": "#include <Python.h>\n",
        }) as root:
            files = list(scan_common.discover_c_files(root))
            suffixes = {f.suffix for f in files}
            self.assertIn(".c", suffixes)
            self.assertIn(".cpp", suffixes)
            self.assertIn(".cxx", suffixes)

    def test_parse_bytes_for_file_c_fallback(self):
        """parse_bytes_for_file works for C files regardless of cpp support."""
        code = b"int main(void) { return 0; }"
        tree = ts.parse_bytes_for_file(code, Path("test.c"))
        self.assertIsNotNone(tree)
        self.assertEqual(tree.root_node.type, "translation_unit")


if __name__ == "__main__":
    unittest.main()
