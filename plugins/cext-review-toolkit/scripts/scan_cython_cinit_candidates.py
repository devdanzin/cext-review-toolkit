#!/usr/bin/env python3
"""scan_cython_cinit_candidates.py — Query 4: detect `__cinit__`/`__init__`
reinit-leak patterns in `cdef class` bodies.

Two detection rules are applied:

**Shape A: overlap-based (cymem-shape)** -- the original Q4 rule.

    cdef class Address:
        cdef void* ptr

        def __cinit__(self, size_t n):
            self.ptr = NULL

        def __init__(self, size_t n):
            self.ptr = malloc(n)              # __cinit__ also assigns ptr -> leak on re-init

Both `__cinit__` and `__init__` assign the same field; `__init__` does not
free the old value first. Caught by detecting overlapping `self.<field> = ...`
assignments.

**Shape B: pointer-field (blosc2-shape)** -- new in v2.

    cdef class SChunk:
        cdef blosc2_schunk *schunk        # raw C-pointer field

        def __init__(self, ...):
            self.schunk = blosc2_schunk_new(...)  # allocates without free guard

        def __dealloc__(self):
            if self.schunk is not NULL:
                blosc2_schunk_free(self.schunk)

There is no `__cinit__` (or it doesn't touch this field), but `__init__`
allocates into a raw pointer field that `__dealloc__` is responsible for
freeing. A subclass-induced second `__init__` leaks the first allocation.
Caught by inspecting class-body field declarations for pointer types and
correlating with `__init__` allocations + `__dealloc__` ownership.

Cython does NOT auto-free raw C-pointer fields on reassignment (unlike Python
`object` fields, which it auto-DECREFs). Both shapes are most dangerous on
those raw pointer fields.

Confidence calibration:
  - HIGH (Shape A): __init__ assigns a `call` expression to the field (likely
                    allocation -- side effect repeats and leaks).
  - MEDIUM (Shape A): __init__ reassigns the field but the RHS isn't an obvious
                      allocation call. May still leak; needs human review.
  - HIGH (Shape B): pointer-field + __init__ allocates + __dealloc__ frees, no
                    free guard preceding the allocation. Strongest signal --
                    the dealloc proves the maintainer owns the lifecycle.

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
FREE_CALL_NAMES = frozenset(
    {
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
    }
)


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


def get_class_pointer_fields(class_block, source: bytes) -> dict[str, str]:
    """Return {field_name: type_text} for every `cdef <T>* name` field declared
    directly in the class body. Only raw C-pointer fields qualify -- those are
    the ones Cython does NOT auto-free on reassignment.
    """
    pointer_fields: dict[str, str] = {}
    for child in class_block.children:
        if child.type != "cdef_statement":
            continue
        cvar = next((c for c in child.children if c.type == "cvar_def"), None)
        if cvar is None:
            continue
        typed = next((c for c in cvar.children if c.type == "maybe_typed_name"), None)
        if typed is None:
            continue
        # A type_modifier child carries the `*`. Without one, this is not a
        # pointer field (e.g. `cdef int x` or `cdef bint flag`).
        has_pointer = any(c.type == "type_modifier" for c in typed.children)
        if not has_pointer:
            continue
        idents = [c for c in typed.children if c.type == "identifier"]
        if len(idents) < 2:
            continue
        name_node = idents[-1]
        name = u.get_text(name_node, source)
        type_text = (
            source[typed.start_byte : name_node.start_byte]
            .decode("utf-8", errors="replace")
            .strip()
        )
        pointer_fields[name] = type_text
    return pointer_fields


def dealloc_references_field(dealloc_block, field: str, source: bytes) -> bool:
    """True if `self.<field>` text appears anywhere in `__dealloc__`'s body.

    `__dealloc__`'s only purpose is cleanup, so any reference to the field is
    a strong signal the maintainer takes responsibility for freeing it.
    """
    if dealloc_block is None:
        return False
    needle = f"self.{field}"
    return needle in u.get_text(dealloc_block, source)


def init_first_alloc_assignment(init_block, field: str, source: bytes):
    """Return the first `self.<field> = <call>(...)` assignment node in
    `__init__`, or None. The RHS must include a call expression -- a literal
    NULL or an integer constant doesn't count as an allocation.
    """
    fields = get_self_field_assignments(init_block, source)
    if field not in fields:
        return None
    first_assign = fields[field][0]
    if len(first_assign.children) < 3:
        return None
    rhs = first_assign.children[2]
    if not any(n.type == "call" for n in u.walk(rhs)):
        return None
    return first_assign


def init_has_field_use_before(
    init_block, field: str, source: bytes, before_node
) -> bool:
    """True if `__init__` calls anything with `self.<field>` as an argument
    BEFORE `before_node`'s position. Used as a 'free guard' heuristic -- if
    the user does anything with the old value before reassigning, presume
    they handle cleanup correctly.
    """
    needle = f"self.{field}"
    cutoff = before_node.start_byte
    for call in u.find_nodes(init_block, "call"):
        if call.start_byte >= cutoff:
            continue
        for arg in u.get_call_arguments(call):
            if needle in u.get_text(arg, source):
                return True
    return False


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
    dealloc = get_method(class_block, "__dealloc__", source)

    init_body = get_function_body(init) if init is not None else None
    cinit_body = get_function_body(cinit) if cinit is not None else None
    dealloc_body = get_function_body(dealloc) if dealloc is not None else None

    # Track (field) keys already flagged so Shape B doesn't double-emit a
    # finding Shape A already reported.
    flagged_fields: set[str] = set()

    # ---- Shape A: overlap-based (cymem-shape) ----
    if cinit_body is not None and init_body is not None:
        cinit_fields = get_self_field_assignments(cinit_body, source)
        init_fields = get_self_field_assignments(init_body, source)
        overlap = sorted(set(cinit_fields) & set(init_fields))

        for field in overlap:
            if has_free_call_for_field(init_body, field, source):
                continue  # developer already handles cleanup -- skip

            first_assign = init_fields[field][0]
            rhs = first_assign.children[2] if len(first_assign.children) >= 3 else None
            rhs_is_call = rhs is not None and any(n.type == "call" for n in u.walk(rhs))

            if rhs_is_call:
                classification = "FIX"
                confidence = "HIGH"
                severity_note = (
                    "RHS is a function call -- likely allocation. The first "
                    "allocation done in `__cinit__` is leaked when this "
                    "assignment fires."
                )
            else:
                classification = "CONSIDER"
                confidence = "MEDIUM"
                severity_note = (
                    "RHS is not an obvious allocation. May still leak if the "
                    "field is a raw C pointer holding a resource; benign if "
                    "it's a Python `object` field (Cython auto-DECREFs)."
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
                        f"`{class_name}.__init__` reassigns `self.{field}` "
                        f"which `__cinit__` already populated, without freeing "
                        f"the old value first. {severity_note}"
                    ),
                    fix_template=(
                        f"Either (a) move the assignment out of `__init__` "
                        f"(let `__cinit__` own the field), (b) free the old "
                        f"value first: `if self.{field} is not NULL: "
                        f"free(self.{field})`, or (c) make `__init__` a no-op "
                        f"and document that re-initialization is unsupported."
                    ),
                    details={
                        "shape": "overlap",
                        "class": class_name,
                        "field": field,
                        "cinit_line": cinit_fields[field][0].start_point[0] + 1,
                        "init_line": line,
                        "rhs_is_call": rhs_is_call,
                    },
                )
            )
            flagged_fields.add(field)

    # ---- Shape B: pointer-field (blosc2-shape) ----
    # Trigger when: init allocates a raw C-pointer field that __dealloc__ owns,
    # without a free guard preceding the allocation. __cinit__ may be absent.
    if init_body is not None and dealloc_body is not None:
        pointer_fields = get_class_pointer_fields(class_block, source)
        for field, type_text in pointer_fields.items():
            if field in flagged_fields:
                continue  # already reported under Shape A
            if not dealloc_references_field(dealloc_body, field, source):
                continue  # __dealloc__ doesn't manage this field
            alloc_assign = init_first_alloc_assignment(init_body, field, source)
            if alloc_assign is None:
                continue  # __init__ doesn't allocate into this field
            if init_has_field_use_before(init_body, field, source, alloc_assign):
                continue  # presume free guard or other safe handling
            if has_free_call_for_field(init_body, field, source):
                continue  # explicit known-free call somewhere -- safe

            line = alloc_assign.start_point[0] + 1
            column = alloc_assign.start_point[1] + 1

            findings.append(
                u.make_finding(
                    file=path,
                    line=line,
                    column=column,
                    function=f"{class_name}.__init__" if class_name else "__init__",
                    category="cinit_init_reinit_leak",
                    classification="FIX",
                    confidence="HIGH",
                    description=(
                        f"`{class_name}.__init__` allocates the raw C-pointer "
                        f"field `self.{field}` (declared `{type_text} *{field}`) "
                        f"without freeing the prior value first. `__dealloc__` "
                        f"manages this field, proving the maintainer owns its "
                        f"lifecycle -- but a subclass-induced second `__init__` "
                        f"call leaks the first allocation."
                    ),
                    fix_template=(
                        f"Either (a) move the allocation into `__cinit__` "
                        f"(runs exactly once), (b) free the old value first: "
                        f"`if self.{field} is not NULL: <free_func>(self.{field})`, "
                        f"or (c) make `__init__` a no-op and document that "
                        f"re-initialization is unsupported."
                    ),
                    details={
                        "shape": "pointer_field",
                        "class": class_name,
                        "field": field,
                        "field_type": type_text,
                        "init_line": line,
                        "dealloc_line": dealloc.start_point[0] + 1
                        if dealloc is not None
                        else None,
                        "cinit_present": cinit is not None,
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
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0] if __doc__ else ""
    )
    ap.add_argument("target", help=".pyx file or directory to scan")
    ap.add_argument("--max-files", type=int, default=0)
    args = ap.parse_args()
    result = analyze(args.target, max_files=args.max_files)
    json.dump(result, sys.stdout, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
