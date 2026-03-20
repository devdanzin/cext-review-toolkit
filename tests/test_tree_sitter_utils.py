"""Tests for tree_sitter_utils.py — core Tree-sitter parsing utilities."""

import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION, EXTENSION_WITH_TYPE, EXTENSION_WITH_BUGS

ts = import_script("tree_sitter_utils")


# ---------------------------------------------------------------------------
# parse_file / parse_string
# ---------------------------------------------------------------------------

def test_parse_file():
    """Parse a C file, verify tree is not None and has no errors."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        tree = ts.parse_file(root / "myext.c")
        assert tree is not None
        assert tree.root_node is not None
        assert not tree.root_node.has_error


def test_parse_string():
    """Parse a C string."""
    tree = ts.parse_string('int main(void) { return 0; }')
    assert tree is not None
    assert tree.root_node.type == "translation_unit"


# ---------------------------------------------------------------------------
# extract_functions
# ---------------------------------------------------------------------------

def test_extract_functions_simple():
    """Extract from a file with 2-3 functions."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    names = [f["name"] for f in funcs]
    assert "myext_hello" in names
    assert "PyInit_myext" in names
    assert len(funcs) >= 2


def test_extract_functions_with_static():
    """Functions with `static` qualifier are found."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    hello = [f for f in funcs if f["name"] == "myext_hello"][0]
    assert "static" in hello["return_type"]


def test_extract_functions_multiline_signature():
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
    assert len(funcs) == 1
    assert funcs[0]["name"] == "my_func"
    assert "static" in funcs[0]["return_type"]


def test_extract_functions_body_content():
    """Function body is extracted correctly."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    hello = [f for f in funcs if f["name"] == "myext_hello"][0]
    assert "PyUnicode_FromString" in hello["body"]
    assert hello["body_node"] is not None


def test_extract_functions_line_numbers():
    """Start/end lines are 1-indexed."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    for f in funcs:
        assert f["start_line"] >= 1
        assert f["end_line"] >= f["start_line"]


# ---------------------------------------------------------------------------
# extract_struct_initializers
# ---------------------------------------------------------------------------

def test_extract_struct_initializers_method_def():
    """Find PyMethodDef arrays."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    inits = ts.extract_struct_initializers(tree, source_bytes, "PyMethodDef")
    assert len(inits) >= 1
    found = inits[0]
    assert found["variable_name"] == "myext_methods"
    assert found["is_array"] is True
    assert "hello" in found["initializer_text"]


def test_extract_struct_initializers_module_def():
    """Find PyModuleDef structs."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    inits = ts.extract_struct_initializers(tree, source_bytes, "PyModuleDef")
    assert len(inits) >= 1
    assert inits[0]["variable_name"] == "myext_module"


# ---------------------------------------------------------------------------
# extract_static_declarations
# ---------------------------------------------------------------------------

def test_extract_static_declarations():
    """Find file-scope static variables."""
    tree = ts.parse_string(EXTENSION_WITH_BUGS)
    source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
    statics = ts.extract_static_declarations(tree, source_bytes)
    names = [s["name"] for s in statics]
    assert "global_cache" in names
    assert "initialized" in names


def test_extract_static_pyobject():
    """Correctly identify static PyObject * declarations."""
    tree = ts.parse_string(EXTENSION_WITH_BUGS)
    source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
    statics = ts.extract_static_declarations(tree, source_bytes)
    cache = [s for s in statics if s["name"] == "global_cache"]
    assert len(cache) == 1
    assert cache[0]["is_pyobject"] is True
    assert cache[0]["is_pointer"] is True


def test_extract_static_const():
    """Correctly mark const declarations."""
    code = 'static const char *version_string = "1.0";\n'
    tree = ts.parse_string(code)
    source_bytes = code.encode("utf-8")
    statics = ts.extract_static_declarations(tree, source_bytes)
    assert len(statics) == 1
    assert statics[0]["is_const"] is True
    assert statics[0]["name"] == "version_string"


# ---------------------------------------------------------------------------
# find_calls_in_scope
# ---------------------------------------------------------------------------

def test_find_calls_in_scope():
    """Find all calls in a function body."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    hello = [f for f in funcs if f["name"] == "myext_hello"][0]
    calls = ts.find_calls_in_scope(hello["body_node"], source_bytes)
    call_names = [c["function_name"] for c in calls]
    assert "PyUnicode_FromString" in call_names


def test_find_calls_filtered():
    """Find only specific API calls."""
    tree = ts.parse_string(EXTENSION_WITH_BUGS)
    source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
    api_set = {"PyList_New", "PyLong_FromLong", "PyList_Append"}
    calls = ts.find_calls_in_scope(leaky["body_node"], source_bytes, api_names=api_set)
    call_names = [c["function_name"] for c in calls]
    assert "PyList_New" in call_names
    assert "PyLong_FromLong" in call_names
    assert "PyList_Append" in call_names
    # Should not include Py_DECREF since it's not in api_set.
    assert "Py_DECREF" not in call_names


# ---------------------------------------------------------------------------
# find_assignments_in_scope
# ---------------------------------------------------------------------------

def test_find_assignments_in_scope():
    """Find variable assignments."""
    tree = ts.parse_string(EXTENSION_WITH_BUGS)
    source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
    assigns = ts.find_assignments_in_scope(leaky["body_node"], source_bytes)
    vars_assigned = [a["variable"] for a in assigns]
    assert "result" in vars_assigned or "item" in vars_assigned


# ---------------------------------------------------------------------------
# find_return_statements
# ---------------------------------------------------------------------------

def test_find_return_statements():
    """Find return statements with values."""
    tree = ts.parse_string(EXTENSION_WITH_BUGS)
    source_bytes = EXTENSION_WITH_BUGS.encode("utf-8")
    funcs = ts.extract_functions(tree, source_bytes)
    leaky = [f for f in funcs if f["name"] == "leaky_function"][0]
    returns = ts.find_return_statements(leaky["body_node"], source_bytes)
    assert len(returns) >= 2
    values = [r["value_text"] for r in returns]
    assert "NULL" in values
    assert "result" in values


# ---------------------------------------------------------------------------
# find_struct_members
# ---------------------------------------------------------------------------

def test_find_struct_members():
    """Find PyObject * members in a typedef struct."""
    tree = ts.parse_string(EXTENSION_WITH_TYPE)
    source_bytes = EXTENSION_WITH_TYPE.encode("utf-8")
    members = ts.find_struct_members(tree, source_bytes, "MyObj")
    names = [m["name"] for m in members]
    assert "name" in names
    assert "value" in names
    assert "count" in names
    # Check PyObject detection.
    name_member = [m for m in members if m["name"] == "name"][0]
    assert name_member["is_pyobject"] is True
    count_member = [m for m in members if m["name"] == "count"][0]
    assert count_member["is_pyobject"] is False


# ---------------------------------------------------------------------------
# walk_descendants
# ---------------------------------------------------------------------------

def test_walk_descendants():
    """Walk nodes with type filter."""
    tree = ts.parse_string(MINIMAL_EXTENSION)
    source_bytes = MINIMAL_EXTENSION.encode("utf-8")
    # Find all string literals.
    strings = list(ts.walk_descendants(tree.root_node, "string_literal"))
    texts = [ts.get_node_text(s, source_bytes) for s in strings]
    assert any('"hello"' in t for t in texts)
    assert any('"myext"' in t for t in texts)


def test_walk_descendants_no_filter():
    """Walk all descendants without filter."""
    tree = ts.parse_string("int x = 1;")
    all_nodes = list(ts.walk_descendants(tree.root_node))
    assert len(all_nodes) > 1  # Should have multiple nodes.


# ---------------------------------------------------------------------------
# strip_comments
# ---------------------------------------------------------------------------

def test_strip_comments():
    """Remove C comments."""
    code = 'int x = 1; /* block comment */\nint y = 2; // line comment\n'
    cleaned = ts.strip_comments(code)
    assert "block comment" not in cleaned
    assert "line comment" not in cleaned
    assert "int x" in cleaned
    assert "int y" in cleaned


# ---------------------------------------------------------------------------
# get_node_text
# ---------------------------------------------------------------------------

def test_get_node_text():
    """Extract text for a node."""
    code = "int main(void) { return 0; }"
    tree = ts.parse_string(code)
    source_bytes = code.encode("utf-8")
    text = ts.get_node_text(tree.root_node, source_bytes)
    assert text == code
