#!/usr/bin/env python3
"""scan_cython_cinit_candidates.py — Query 4: detect `__cinit__`/`__init__`
field-reassignment patterns that leak resources allocated in `__cinit__`.

The bug pattern (F3-F8 in blosc2, the cymem `Address.__init__` leak):

    cdef class Foo:
        cdef void* ptr

        def __cinit__(self, size_t n):
            self.ptr = malloc(n)              # allocation #1

        def __init__(self, size_t n):
            self.ptr = malloc(n)              # allocation #2 — leaks #1

`__cinit__` runs once at construction. `__init__` is Python-level and may run
multiple times (subclassing, explicit re-call). When `__init__` reassigns a
field that `__cinit__` already populated -- without freeing the old value
first -- the old resource leaks.

This is most dangerous when the field is a raw C pointer (`void*`, `char*`,
`cdef struct*`, etc.) because Cython does NOT auto-free those on reassignment.
For Python `object` fields, Cython's compiler inserts an implicit decref, so
the bug is benign there. The agent triaging this script's output should check
the field's declared type to decide severity.

Detection:
  1. Find every `class_definition` (whether inside a `cdef class` or not).
  2. Within it, find direct-child `function_definition`s named `__cinit__`
     and `__init__`.
  3. Extract the set of fields each method assigns via `self.<field> = ...`.
  4. For each field assigned in BOTH methods, check whether `__init__`
     contains a free/decref call referencing that field. If not, emit a
     candidate.

Confidence calibration:
  - HIGH    : __init__ assigns a `call` expression to the field (likely
              allocation -- side effect repeats and leaks).
  - MEDIUM  : __init__ reassigns the field but the RHS isn't an obvious
              allocation call. May still leak; needs human review.

Calling convention matches existing scripts:
    analyze(target: str, *, max_files: int = 0) -> dict
    JSON output to stdout via main()
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cython_ast_utils as u

# Free/decref calls that, if present in __init__, suggest the developer is
# already handling old-value cleanup before reassigning.
FREE_CALL_NAMES = frozenset({
    "free",
    "PyMem_Free",
    "PyMem_RawFree",
    "PyObject_Free",
    "PyObject_GC_Del",
    "Py_DECREF",
    "Py_XDECREF",
    "Py_CLEAR",
    "Py_XSETREF",
    "Py_SETREF",
})


def get_self_field_assignments(block_node, source: bytes) -> dict[str, list]:
    """For a method's body block, return a mapping of {field_name: [assignment_node, ...]}
    where the LHS is `self.<field_name>`.
    """
    result: dict[str, list] = {}
    for assign in u.find_nodes(block_node, "assignment"):
        if not assign.children:
            continue
        lhs = assign.children[0]
        if lhs.type != "attribute":
            continue
        # attribute structure: identifier "." identifier (possibly nested)
        # We want exactly `self.<field>` -- two identifiers around a single dot.
        ident_children = [c for c in lhs.children if c.type == "identifier"]
        if len(ident_children) != 2:
            continue
        if u.get_text(ident_children[0], source) != "self":
            continue
        field = u.get_text(ident_children[1], source)
        result.setdefault(field, []).append(assign)
    return result


def has_free_call_for_field(block_node, field: str, source: bytes) -> bool:
    """True if the method body contains a free/decref-style call whose argument
    references `self.<field>`.
    """
    needle = f"self.{field}"
    for call in u.find_nodes(block_node, "call"):
        name = u.get_call_name(call, source)
        if name not in FREE_CALL_NAMES:
            continue
        # Check whether any argument text contains `self.<field>`
        for arg in u.get_call_arguments(call):
            if needle in u.get_text(arg, source):
                return True
    return False


def get_method(class_block, method_name: str, source: bytes):
    """Return the `function_definition` node for `def <method_name>(...)` directly
    inside `class_block`, or None.
    """
    for child in class_block.children:
        if child.type != "function_definition":
            continue
        for c in child.children:
            if c.type == "identifier":
                if u.get_text(c, source) == method_name:
                    return child
                break
    return None


def get_function_body(fn_def):
    """Return the `block` child of a `function_definition`, or None."""
    for c in fn_def.children:
        if c.type == "block":
            return c
    return None


def analyze_class(class_def, source: bytes, path: Path) -> list[dict]:
    """Analyze a single class_definition node for cinit/init reinit-leak."""
    findings: list[dict] = []

    # Find the class body block
    class_block = None
    class_name = None
    for c in class_def.children:
        if c.type == "block":
            class_block = c
        elif c.type == "identifier" and class_name is None:
            class_name = u.get_text(c, source)
    if class_block is None:
        return findings

    cinit = get_method(class_block, "__cinit__", source)
    init = get_method(class_block, "__init__", source)
    if cinit is None or init is None:
        return findings

    cinit_body = get_function_body(cinit)
    init_body = get_function_body(init)
    if cinit_body is None or init_body is None:
        return findings

    cinit_fields = get_self_field_assignments(cinit_body, source)
    init_fields = get_self_field_assignments(init_body, source)

    overlap = sorted(set(cinit_fields) & set(init_fields))
    if not overlap:
        return findings

    for field in overlap:
        if has_free_call_for_field(init_body, field, source):
            continue  # developer already handles cleanup -- skip

        # Inspect the RHS of the FIRST __init__ assignment to gauge severity
        first_assign = init_fields[field][0]
        # children: [LHS, "=", RHS]
        rhs = first_assign.children[2] if len(first_assign.children) >= 3 else None
        rhs_is_call = rhs is not None and any(
            n.type == "call" for n in u.walk(rhs)
        )

        if rhs_is_call:
            classification = "FIX"
            confidence = "HIGH"
            severity_note = (
                "RHS is a function call -- likely allocation. The first "
                "allocation done in `__cinit__` is leaked when this assignment "
                "fires."
            )
        else:
            classification = "CONSIDER"
            confidence = "MEDIUM"
            severity_note = (
                "RHS is not an obvious allocation. May still leak if the field "
                "is a raw C pointer holding a resource; benign if it's a Python "
                "`object` field (Cython auto-DECREFs)."
            )

        line = first_assign.start_point[0] + 1
        column = first_assign.start_point[1] + 1

        findings.append(
            u.make_finding(
                file=path,
                line=line,
                column=column,
                function=f"{class_name}.__init__" if class_name else "__init__",
                category="cinit_init_reinit_leak",
                classification=classification,
                confidence=confidence,
                description=(
                    f"`{class_name}.__init__` reassigns `self.{field}` which "
                    f"`__cinit__` already populated, without freeing the old "
                    f"value first. {severity_note}"
                ),
                fix_template=(
                    f"Either (a) move the assignment out of `__init__` (let "
                    f"`__cinit__` own the field), (b) free the old value "
                    f"first: `if self.{field} is not NULL: free(self.{field})`, "
                    f"or (c) make `__init__` a no-op and document that "
                    f"re-initialization is unsupported."
                ),
                details={
                    "class": class_name,
                    "field": field,
                    "cinit_line": cinit_fields[field][0].start_point[0] + 1,
                    "init_line": line,
                    "rhs_is_call": rhs_is_call,
                },
            )
        )

    return findings


def analyze_file(path: Path, source: bytes) -> list[dict]:
    tree = u.parse_bytes(source)
    findings: list[dict] = []
    for class_def in u.find_nodes(tree.root_node, "class_definition"):
        findings.extend(analyze_class(class_def, source, path))
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    files = u.find_pyx_files(target, max_files=max_files)
    all_findings: list[dict] = []
    parse_errors = 0

    for path in files:
        source = path.read_bytes()
        try:
            all_findings.extend(analyze_file(path, source))
        except Exception as e:
            parse_errors += 1
            print(f"WARNING: failed to analyze {path}: {e}", file=sys.stderr)

    return {
        "script": "scan_cython_cinit_candidates",
        "target": str(target),
        "findings": all_findings,
        "stats": {
            "files_scanned": len(files),
            "candidates": len(all_findings),
            "parse_errors": parse_errors,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    ap.add_argument("target", help=".pyx file or directory to scan")
    ap.add_argument("--max-files", type=int, default=0)
    args = ap.parse_args()
    result = analyze(args.target, max_files=args.max_files)
    json.dump(result, sys.stdout, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
