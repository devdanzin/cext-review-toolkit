"""Tests for tree_sitter_utils.py — core Tree-sitter parsing utilities."""

import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_TYPE, EXTENSION_WITH_BUGS

ts = import_script("tree_sitter_utils")


class TestTreeSitterUtils(unittest.TestCase):
    """Test Tree-sitter parsing utilities."""

    def test_parse_file(self):
        """Parse a C file, verify tree is not None and has no errors."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            tree = ts.parse_file(root / "myext.c")
            self.assertIsNotNone(tree)
            self.assertIsNotNone(tree.root_node)
            self.assertFalse(tree.root_node.has_error)

    def test_parse_string(self):
        """Parse a C string."""
        tree = ts.parse_string('int main(void) { return 0; }')
        self.assertIsNotNone(tree)
        self.assertEqual(tree.root_node.type, "translation_unit")

    def test_extract_functions_simple(self):
        """Extract from a file with 2-3 functions."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        names = [f["name"] for f in funcs]
        self.assertIn("myext_hello", names)
        self.assertIn("PyInit_myext", names)
        self.assertGreaterEqual(len(funcs), 2)

    def test_extract_functions_with_static(self):
        """Functions with `static` qualifier are found."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        hello = [f for f in funcs if f["name"] == "myext_hello"][0]
        self.assertIn("static", hello["return_type"])

    def test_extract_functions_multiline_signature(self):
        """Return type on separate line from function name."""
        code = """\
static PyObject *
my_func(PyObject *self, PyObject *args)
{
    return Py_None;
}
"""
        tree = ts.parse_string(code)
        funcs = ts.extract_functions(tree, code.encode("utf-8"))
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "my_func")
        self.assertIn("static", funcs[0]["return_type"])

    def test_extract_functions_body_content(self):
        """Function body is extracted correctly."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        hello = [f for f in funcs if f["name"] == "myext_hello"][0]
        self.assertIn("PyUnicode_FromString", hello["body"])
        self.assertIsNotNone(hello["body_node"])

    def test_extract_functions_line_numbers(self):
        """Start/end lines are 1-indexed."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        for f in funcs:
            self.assertGreaterEqual(f["start_line"], 1)
            self.assertGreaterEqual(f["end_line"], f["start_line"])

    def test_extract_struct_initializers_method_def(self):
        """Find PyMethodDef arrays."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        inits = ts.extract_struct_initializers(tree, source_bytes, "PyMethodDef")
        self.assertGreaterEqual(len(inits), 1)
        found = inits[0]
        self.assertEqual(found["variable_name"], "myext_methods")
        self.assertTrue(found["is_array"])
        self.assertIn("hello", found["initializer_text"])

    def test_extract_struct_initializers_module_def(self):
        """Find PyModuleDef structs."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        inits = ts.extract_struct_initializers(tree, source_bytes, "PyModuleDef")
        self.assertGreaterEqual(len(inits), 1)
        self.assertEqual(inits[0]["variable_name"], "myext_module")

    def test_extract_static_declarations(self):
        """Find file-scope static variables."""
        tree = ts.parse_string(EXTENSION_WITH_BUGS)
        source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
        statics = ts.extract_static_declarations(tree, source_bytes)
        names = [s["name"] for s in statics]
        self.assertIn("global_cache", names)
        self.assertIn("initialized", names)

    def test_extract_static_pyobject(self):
        """Correctly identify static PyObject * declarations."""
        tree = ts.parse_string(EXTENSION_WITH_BUGS)
        source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
        statics = ts.extract_static_declarations(tree, source_bytes)
        cache = [s for s in statics if s["name"] == "global_cache"]
        self.assertEqual(len(cache), 1)
        self.assertTrue(cache[0]["is_pyobject"])
        self.assertTrue(cache[0]["is_pointer"])

    def test_extract_static_const(self):
        """Correctly mark const declarations."""
        code = 'static const char *version_string = "1.0";\n'
        tree = ts.parse_string(code)
        source_bytes = code.encode("utf-8")
        statics = ts.extract_static_declarations(tree, source_bytes)
        self.assertEqual(len(statics), 1)
        self.assertTrue(statics[0]["is_const"])
        self.assertEqual(statics[0]["name"], "version_string")

    def test_find_calls_in_scope(self):
        """Find all calls in a function body."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        hello = [f for f in funcs if f["name"] == "myext_hello"][0]
        calls = ts.find_calls_in_scope(hello["body_node"], source_bytes)
        call_names = [c["function_name"] for c in calls]
        self.assertIn("PyUnicode_FromString", call_names)

    def test_find_calls_filtered(self):
        """Find only specific API calls."""
        tree = ts.parse_string(EXTENSION_WITH_BUGS)
        source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
        api_set = {"PyList_New", "PyLong_FromLong", "PyList_Append"}
        calls = ts.find_calls_in_scope(leaky["body_node"], source_bytes, api_names=api_set)
        call_names = [c["function_name"] for c in calls]
        self.assertIn("PyList_New", call_names)
        self.assertIn("PyLong_FromLong", call_names)
        self.assertIn("PyList_Append", call_names)
        self.assertNotIn("Py_DECREF", call_names)

    def test_find_assignments_in_scope(self):
        """Find variable assignments."""
        tree = ts.parse_string(EXTENSION_WITH_BUGS)
        source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
        assigns = ts.find_assignments_in_scope(leaky["body_node"], source_bytes)
        vars_assigned = [a["variable"] for a in assigns]
        self.assertTrue("result" in vars_assigned or "item" in vars_assigned)

    def test_find_return_statements(self):
        """Find return statements with values."""
        tree = ts.parse_string(EXTENSION_WITH_BUGS)
        source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
        funcs = ts.extract_functions(tree, source_bytes)
        leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
        returns = ts.find_return_statements(leaky["body_node"], source_bytes)
        self.assertGreaterEqual(len(returns), 2)
        values = [r["value_text"] for r in returns]
        self.assertIn("NULL", values)
        self.assertIn("result", values)

    def test_find_struct_members(self):
        """Find PyObject * members in a typedef struct."""
        tree = ts.parse_string(EXTENSION_WITH_TYPE)
        source_bytes = EXTENSION_WITH_TYPE.encode("utf-8")
        members = ts.find_struct_members(tree, source_bytes, "MyObj")
        names = [m["name"] for m in members]
        self.assertIn("name", names)
        self.assertIn("value", names)
        self.assertIn("count", names)
        name_member = [m for m in members if m["name"] == "name"][0]
        self.assertTrue(name_member["is_pyobject"])
        count_member = [m for m in members if m["name"] == "count"][0]
        self.assertFalse(count_member["is_pyobject"])

    def test_walk_descendants(self):
        """Walk nodes with type filter."""
        tree = ts.parse_string(MINIMAL_EXTENSION)
        source_bytes = MINIMAL_EXTENSION.encode("utf-8")
        strings = list(ts.walk_descendants(tree.root_node, "string_literal"))
        texts = [ts.get_node_text(s, source_bytes) for s in strings]
        self.assertTrue(any('"hello"' in t for t in texts))
        self.assertTrue(any('"myext"' in t for t in texts))

    def test_walk_descendants_no_filter(self):
        """Walk all descendants without filter."""
        tree = ts.parse_string("int x = 1;")
        all_nodes = list(ts.walk_descendants(tree.root_node))
        self.assertGreater(len(all_nodes), 1)

    def test_strip_comments(self):
        """Remove C comments."""
        code = 'int x = 1; /* block comment */\nint y = 2; // line comment\n'
        cleaned = ts.strip_comments(code)
        self.assertNotIn("block comment", cleaned)
        self.assertNotIn("line comment", cleaned)
        self.assertIn("int x", cleaned)
        self.assertIn("int y", cleaned)

    def test_get_node_text(self):
        """Extract text for a node."""
        code = "int main(void) { return 0; }"
        tree = ts.parse_string(code)
        source_bytes = code.encode("utf-8")
        text = ts.get_node_text(tree.root_node, source_bytes)
        self.assertEqual(text, code)


if __name__ == "__main__":
    unittest.main()
