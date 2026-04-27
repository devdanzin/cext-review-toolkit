"""Tests for cython_ast_utils.py — Tree-sitter-cython parsing utilities.

Requires tree-sitter-cython (devdanzin fork). If not installed, this test
module fails to collect -- matching the project convention for tests that
need a parser dependency (see test_tree_sitter_utils.py).
"""

import unittest

from helpers import import_script

u = import_script("cython_ast_utils")


class TestParsing(unittest.TestCase):
    def test_parse_string_returns_tree(self):
        tree = u.parse_string("cdef int x = 1\n")
        self.assertIsNotNone(tree)
        self.assertEqual(tree.root_node.type, "module")

    def test_parse_bytes_roundtrip(self):
        src = b"cdef int x = 1\n"
        tree = u.parse_bytes(src)
        self.assertIsNotNone(tree)
        self.assertFalse(u.has_parse_errors(tree))

    def test_has_parse_errors_clean(self):
        tree = u.parse_string("def foo():\n    return 1\n")
        self.assertFalse(u.has_parse_errors(tree))

    def test_has_parse_errors_dirty(self):
        # Deliberately malformed input -- closing paren without opening.
        tree = u.parse_string("def foo)):\n    pass\n")
        self.assertTrue(u.has_parse_errors(tree))


class TestCdefFunctionDetection(unittest.TestCase):
    """is_cdef_function vs is_cdef_function_pointer_field."""

    def test_real_definition_with_block(self):
        src = b"cdef int real(int x):\n    return x\n"
        tree = u.parse_bytes(src)
        cdefs = list(u.find_nodes(tree.root_node, "cdef_statement"))
        self.assertEqual(len(cdefs), 1)
        self.assertTrue(u.is_cdef_function(cdefs[0]))
        self.assertFalse(u.is_cdef_function_pointer_field(cdefs[0]))

    def test_function_pointer_field_no_block(self):
        # Function-pointer field: parameters but no body
        src = b"cdef struct S:\n    int (*forecasting)(int x)\n"
        tree = u.parse_bytes(src)
        cdefs = list(u.find_nodes(tree.root_node, "cdef_statement"))
        # The inner field is a cdef_statement; it should NOT be a function definition
        for c in cdefs:
            self.assertFalse(u.is_cdef_function(c))


class TestReturnTypeExtraction(unittest.TestCase):
    def test_simple_int_return(self):
        src = b"cdef int foo(int x):\n    return x\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        self.assertEqual(u.get_cdef_function_return_text(cdef, src), "int")

    def test_pointer_return_includes_modifier(self):
        src = b"cdef char* foo():\n    return NULL\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        # Pointer returns should include the asterisk in the extracted text
        rt = u.get_cdef_function_return_text(cdef, src)
        self.assertIn("*", rt)

    def test_no_return_type(self):
        # `cdef foo():` -- the parser may treat this as no explicit return type;
        # the helper returns "" in that case.
        src = b"def foo():\n    return 1\n"
        tree = u.parse_bytes(src)
        # def is function_definition, not cdef_statement -- helper returns ""
        # for non-cdef nodes
        for c in u.find_nodes(tree.root_node, "function_definition"):
            self.assertEqual(u.get_cdef_function_return_text(c, src), "")


class TestExceptionAndGilFlags(unittest.TestCase):
    def test_has_exception_value_present(self):
        src = b"cdef int foo() except -1:\n    return 0\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        _ret, _name, fn_def = u.get_cdef_function_parts(cdef)
        self.assertTrue(u.has_exception_value(fn_def))

    def test_has_exception_value_absent(self):
        src = b"cdef int foo():\n    return 0\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        _ret, _name, fn_def = u.get_cdef_function_parts(cdef)
        self.assertFalse(u.has_exception_value(fn_def))

    def test_has_noexcept(self):
        src = b"cdef int foo() noexcept:\n    return 0\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        _ret, _name, fn_def = u.get_cdef_function_parts(cdef)
        self.assertTrue(u.has_noexcept(fn_def, src))

    def test_has_nogil(self):
        src = b"cdef int foo() nogil:\n    return 0\n"
        tree = u.parse_bytes(src)
        cdef = next(u.find_nodes(tree.root_node, "cdef_statement"))
        _ret, _name, fn_def = u.get_cdef_function_parts(cdef)
        self.assertTrue(u.has_nogil(fn_def, src))


class TestCallHelpers(unittest.TestCase):
    def test_get_call_name_simple(self):
        src = b"def foo():\n    bar(1, 2)\n"
        tree = u.parse_bytes(src)
        call = next(u.find_nodes(tree.root_node, "call"))
        self.assertEqual(u.get_call_name(call, src), "bar")

    def test_get_call_name_attribute(self):
        src = b"def foo():\n    self.bar(1)\n"
        tree = u.parse_bytes(src)
        call = next(u.find_nodes(tree.root_node, "call"))
        self.assertEqual(u.get_call_name(call, src), "self.bar")

    def test_get_call_arguments_count(self):
        src = b"def foo():\n    bar(a, b, c)\n"
        tree = u.parse_bytes(src)
        call = next(u.find_nodes(tree.root_node, "call"))
        args = u.get_call_arguments(call)
        self.assertEqual(len(args), 3)


class TestFindEnclosing(unittest.TestCase):
    def test_finds_function_ancestor(self):
        src = b"def foo():\n    bar(1)\n"
        tree = u.parse_bytes(src)
        call = next(u.find_nodes(tree.root_node, "call"))
        fn = u.find_enclosing(call, ["function_definition"])
        self.assertIsNotNone(fn)
        self.assertEqual(fn.type, "function_definition")

    def test_returns_none_when_no_match(self):
        src = b"x = 1\n"
        tree = u.parse_bytes(src)
        ident = next(u.find_nodes(tree.root_node, "identifier"))
        self.assertIsNone(u.find_enclosing(ident, ["function_definition"]))


if __name__ == "__main__":
    unittest.main()
