#!/usr/bin/env python3
"""Tree-sitter parsing utilities for Cython source analysis.

This is the core parsing module used by all Cython analysis scripts in
cext-review-toolkit. It provides structured access to .pyx source code
via tree-sitter-cython, replacing fragile regex-based parsing.

Requires: pip install tree-sitter
          pip install git+https://github.com/devdanzin/tree-sitter-cython.git@fix/grammar-gaps
          (the upstream b0o/tree-sitter-cython has 19 grammar gaps that the
           devdanzin fork on the fix/grammar-gaps branch closes; see
           comms/tree-sitter-cython-fixes/ for the gap catalogue)

Phase 0 calibration confirmed zero parse errors across a 51-file corpus
(cymem, cytoolz, h3-py, uvloop, blosc2, msgpack-python, lxml, statsmodels)
with the fixed parser.
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path
from typing import Iterable, Iterator

# Suppress the deprecated-int-argument warning from tree_sitter.Language(int)
# which is a known cosmetic issue in the current tree-sitter / tree_sitter_cython API.
# Apply BEFORE imports so the module-level Language() call below is silent.
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import tree_sitter
    import tree_sitter_cython as _tscy
except ImportError:
    print(
        json.dumps(
            {
                "error": "tree-sitter-cython not installed",
                "install": (
                    "pip install tree-sitter && "
                    "pip install git+https://github.com/devdanzin/tree-sitter-cython.git@fix/grammar-gaps"
                ),
            }
        )
    )
    sys.exit(1)

CYTHON_LANGUAGE = tree_sitter.Language(_tscy.language())
_parser = tree_sitter.Parser(CYTHON_LANGUAGE)

PYX_EXTENSIONS = frozenset({".pyx", ".pxd", ".pxi"})


# ---------------------------------------------------------------------------
# Parsing helpers


def parse_file(path: Path) -> tree_sitter.Tree:
    """Parse a .pyx source file and return the tree-sitter syntax tree."""
    return _parser.parse(path.read_bytes())


def parse_bytes(source_bytes: bytes) -> tree_sitter.Tree:
    """Parse Cython source from bytes already in memory."""
    return _parser.parse(source_bytes)


def parse_string(source: str) -> tree_sitter.Tree:
    """Parse a Cython source string (encodes as UTF-8 first)."""
    return _parser.parse(source.encode("utf-8"))


def has_parse_errors(tree: tree_sitter.Tree) -> bool:
    """Return True if the tree contains any ERROR or missing nodes.

    Cython 4 may add new syntax that the parser doesn't handle. Scripts should
    check this and either skip the file or fall back to regex.
    """
    for node in walk(tree.root_node):
        if node.type == "ERROR" or node.is_missing:
            return True
    return False


def find_pyx_files(target: str | Path, max_files: int = 0) -> list[Path]:
    """Find .pyx (and .pxd, .pxi) files under a target path.

    `target` may be a single file or a directory. Recursively walks directories,
    skipping `build/`, `tests/`, and hidden dirs.
    """
    p = Path(target)
    if p.is_file() and p.suffix in PYX_EXTENSIONS:
        return [p]
    if not p.is_dir():
        return []

    results: list[Path] = []
    skip_parts = {"build", "tests", "test", "_deps", ".git", "__pycache__", "site-packages"}
    for f in p.rglob("*"):
        if f.suffix not in PYX_EXTENSIONS:
            continue
        if any(part.startswith(".") or part in skip_parts for part in f.parts):
            continue
        results.append(f)
        if max_files and len(results) >= max_files:
            break
    return sorted(results)


# ---------------------------------------------------------------------------
# AST walking


def walk(node: tree_sitter.Node) -> Iterator[tree_sitter.Node]:
    """Pre-order walk of every node in the subtree rooted at `node`."""
    yield node
    for child in node.children:
        yield from walk(child)


def find_nodes(node: tree_sitter.Node, type_name: str) -> Iterator[tree_sitter.Node]:
    """Yield every descendant of `node` whose type is `type_name`."""
    for n in walk(node):
        if n.type == type_name:
            yield n


def find_nodes_any(node: tree_sitter.Node, type_names: Iterable[str]) -> Iterator[tree_sitter.Node]:
    """Yield every descendant whose type is in `type_names`."""
    type_set = frozenset(type_names)
    for n in walk(node):
        if n.type in type_set:
            yield n


def get_text(node: tree_sitter.Node, source: bytes) -> str:
    """Extract source text for a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def get_line_text(source: bytes, line_no_zero_indexed: int) -> str:
    """Get a single source line (0-indexed) from the byte buffer."""
    lines = source.split(b"\n")
    if 0 <= line_no_zero_indexed < len(lines):
        return lines[line_no_zero_indexed].decode("utf-8", errors="replace")
    return ""


def find_enclosing(node: tree_sitter.Node, type_names: Iterable[str]) -> tree_sitter.Node | None:
    """Walk up from `node` to find the nearest ancestor of any given type.

    Used to locate the enclosing function (`function_definition`, `cdef_statement`)
    for control-flow analysis like buffer-protocol pairing.
    """
    type_set = frozenset(type_names)
    cur = node.parent
    while cur is not None:
        if cur.type in type_set:
            return cur
        cur = cur.parent
    return None


# ---------------------------------------------------------------------------
# Cython-specific structural helpers


def is_cdef_function(node: tree_sitter.Node) -> bool:
    """True if `node` is a `cdef_statement` whose body is a function DEFINITION
    (i.e., has an actual block body, not just a declaration or function-pointer
    field).

    Distinguishes:
        cdef int real_func(int x):       # has block → True
            return 0
    From:
        cdef int (*forecasting)(int x)   # no block → False (function-pointer field)
        cdef int forward_decl(int x)     # no block → False (forward declaration in .pxd)
    """
    if node.type != "cdef_statement":
        return False
    for child in node.children:
        if child.type == "cvar_def":
            for grandchild in child.children:
                if grandchild.type == "c_function_definition":
                    # Require a `block` child to count as a real definition
                    if any(c.type == "block" for c in grandchild.children):
                        return True
    return False


def is_cdef_function_pointer_field(node: tree_sitter.Node) -> bool:
    """True if `node` is a `cdef_statement` declaring a function-pointer field
    or forward declaration (parameters but no body). Useful for separately
    auditing function-pointer types when relevant.
    """
    if node.type != "cdef_statement":
        return False
    for child in node.children:
        if child.type == "cvar_def":
            for grandchild in child.children:
                if grandchild.type == "c_function_definition":
                    if not any(c.type == "block" for c in grandchild.children):
                        return True
    return False


def get_cdef_function_parts(
    cdef_stmt: tree_sitter.Node,
) -> tuple[tree_sitter.Node | None, tree_sitter.Node | None, tree_sitter.Node | None]:
    """For a `cdef_statement` containing a function definition, return
    (return_type_node, name_node, c_function_definition_node).

    Returns (None, None, None) if the structure isn't a function definition.
    """
    if cdef_stmt.type != "cdef_statement":
        return (None, None, None)

    cvar_def = next((c for c in cdef_stmt.children if c.type == "cvar_def"), None)
    if cvar_def is None:
        return (None, None, None)

    typed_name = next((c for c in cvar_def.children if c.type == "maybe_typed_name"), None)
    func_def = next((c for c in cvar_def.children if c.type == "c_function_definition"), None)
    if typed_name is None or func_def is None:
        return (None, None, None)

    # maybe_typed_name has variable child shape:
    #   [identifier]                          → no return type, just name
    #   [type_node, identifier]               → simple type + name
    #   [identifier, type_modifier, identifier] → pointer type, e.g. `void* alloc`
    #   [identifier, type_modifier, type_modifier, identifier] → `int** name`
    # The LAST identifier is always the name; everything before is the return type.
    return_type_node = None
    name_node = None
    children = typed_name.children
    if len(children) == 1:
        name_node = children[0]
    elif len(children) >= 2:
        # Last child is the name; everything before constitutes the return type
        name_node = children[-1]
        # For simple T name → return_type_node is the single non-name child.
        # For pointer types we'll capture the first type-bearing child as a
        # representative; callers wanting exact return-type text should slice
        # the source between typed_name.start_byte and name_node.start_byte.
        return_type_node = children[0]

    return (return_type_node, name_node, func_def)


def get_cdef_function_return_text(cdef_stmt: tree_sitter.Node, source: bytes) -> str:
    """Extract the full return-type text (including pointer modifiers) for a
    cdef function. Returns '' for functions without an explicit return type.
    """
    if cdef_stmt.type != "cdef_statement":
        return ""
    cvar_def = next((c for c in cdef_stmt.children if c.type == "cvar_def"), None)
    if cvar_def is None:
        return ""
    typed_name = next((c for c in cvar_def.children if c.type == "maybe_typed_name"), None)
    if typed_name is None or not typed_name.children:
        return ""
    if len(typed_name.children) == 1:
        return ""  # only the name, no return type
    # Slice from typed_name.start_byte to the LAST child's start_byte
    name_node = typed_name.children[-1]
    return source[typed_name.start_byte : name_node.start_byte].decode("utf-8", errors="replace").strip()


def has_exception_value(c_function_def: tree_sitter.Node) -> bool:
    """True if a c_function_definition has an `exception_value` child
    (i.e., includes an `except` / `except *` / `except -1` / `except? 0` clause).
    """
    return any(c.type == "exception_value" for c in c_function_def.children)


def has_noexcept(c_function_def: tree_sitter.Node, source: bytes) -> bool:
    """True if a c_function_definition has a `noexcept` keyword.

    The Cython grammar may represent this as a separate node or as part of
    the function body's modifiers; this helper matches by text on the
    function-signature line.
    """
    # Get the function signature text (everything before the colon)
    text = get_text(c_function_def, source)
    # Strip body, keep signature
    colon_idx = text.find(":")
    sig = text[:colon_idx] if colon_idx >= 0 else text
    return bool(re.search(r"\bnoexcept\b", sig))


def has_nogil(c_function_def: tree_sitter.Node, source: bytes) -> bool:
    """True if the function signature contains `nogil`."""
    text = get_text(c_function_def, source)
    colon_idx = text.find(":")
    sig = text[:colon_idx] if colon_idx >= 0 else text
    return bool(re.search(r"\bnogil\b", sig))


def get_call_name(call_node: tree_sitter.Node, source: bytes) -> str | None:
    """For a `call` node, return the called function's name (text)."""
    if call_node.type != "call":
        return None
    if not call_node.children:
        return None
    # First child is typically the callee
    callee = call_node.children[0]
    if callee.type == "identifier":
        return get_text(callee, source)
    # Attribute access (mod.func) — return full dotted name
    if callee.type == "attribute":
        return get_text(callee, source)
    return None


def get_call_arguments(call_node: tree_sitter.Node) -> list[tree_sitter.Node]:
    """For a `call` node, return its argument nodes (excluding parentheses + commas)."""
    if call_node.type != "call":
        return []
    arg_list = next((c for c in call_node.children if c.type == "argument_list"), None)
    if arg_list is None:
        return []
    return [c for c in arg_list.children if c.type not in {"(", ")", ","}]


# ---------------------------------------------------------------------------
# Common output schema


def make_finding(
    *,
    file: Path,
    line: int,
    column: int,
    function: str | None,
    category: str,
    classification: str,  # FIX | CONSIDER | POLICY | ACCEPTABLE
    confidence: str,  # HIGH | MEDIUM | LOW
    description: str,
    fix_template: str | None = None,
    details: dict | None = None,
) -> dict:
    """Construct a finding dict with the common output schema."""
    return {
        "file": str(file),
        "line": line,
        "column": column,
        "function": function,
        "category": category,
        "classification": classification,
        "confidence": confidence,
        "description": description,
        "fix_template": fix_template,
        "details": details or {},
    }


def empty_envelope(script: str, target: str) -> dict:
    """Empty result envelope — same shape as existing scripts."""
    return {
        "script": script,
        "target": target,
        "findings": [],
        "stats": {"files_scanned": 0, "candidates": 0, "parse_errors": 0},
    }
