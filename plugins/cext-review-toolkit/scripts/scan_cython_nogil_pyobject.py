#!/usr/bin/env python3
"""scan_cython_nogil_pyobject.py — Query 5: detect Python-level operations
inside `nogil` contexts that don't re-acquire the GIL via `with gil:`.

Cython lets you write `cdef T foo(...) nogil:` or `with nogil:` blocks where
the GIL is released. Touching the Python C API in those scopes is undefined
behavior unless you re-enter `with gil:` first. The Cython compiler catches
many cases at compile time, but several patterns slip through (especially
when the operation is hidden behind a helper or when `except *` makes raises
implicitly grab the GIL only at the boundary, not for in-body sequencing).

Patterns flagged:
  - `raise <expr>` directly inside a nogil scope (without `with gil:` between).
    Cython lets bare `raise` work in `nogil` functions with `except *` by
    implicitly acquiring the GIL at the raise, but that's a footgun: the
    expression construction (`ValueError("foo %d" % x)`) happens before the
    raise and may itself need the GIL.
  - `print(...)` calls.
  - f-strings (`joined_str` nodes) — formatting goes through Python.
  - Comprehensions (list/dict/set/generator) — all Python-level.

Detection algorithm:
  1. Find every Python-touching candidate node anywhere in the tree.
  2. Walk up to the nearest GIL-determining ancestor:
       - `c_function_definition` with `gil_spec` child containing `nogil` → nogil scope
       - `with_statement` whose first item is `nogil`                    → nogil scope
       - `with_statement` whose first item is `gil`                       → safe (skip)
  3. If the determining ancestor is a nogil scope, emit a candidate.

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

# Python-level builtins whose use in a nogil scope is almost certainly wrong.
# Conservative list -- we'd rather miss some than flag every C function call.
PYTHON_BUILTINS = frozenset({
    "print",
    "repr",
    "input",
    "open",
    "compile",
    "eval",
    "exec",
})

COMPREHENSION_TYPES = frozenset({
    "list_comprehension",
    "dict_comprehension",
    "set_comprehension",
    "generator_expression",
})


def with_statement_kind(with_node, source: bytes) -> str | None:
    """For a `with_statement` node, return 'gil', 'nogil', or None."""
    if with_node.type != "with_statement":
        return None
    for c in with_node.children:
        if c.type != "with_clause":
            continue
        for item in c.children:
            if item.type != "with_item":
                continue
            for ic in item.children:
                if ic.type == "identifier":
                    text = u.get_text(ic, source)
                    if text in ("gil", "nogil"):
                        return text
            return None
    return None


def cdef_function_is_nogil(c_func_def, source: bytes) -> bool:
    """True if a `c_function_definition` node has `nogil` in its gil_spec."""
    for c in c_func_def.children:
        if c.type == "gil_spec":
            if any(g.type == "nogil" for g in c.children):
                return True
            # Some grammars stash it as identifier text
            if "nogil" in u.get_text(c, source):
                return True
    return False


def determining_gil_scope(node, source: bytes) -> str | None:
    """Walk up from `node` until we find the determining GIL scope. Returns:
        'nogil'  — node is in a nogil scope (with no intervening `with gil:`)
        'gil'    — node is in a `with gil:` block (safe)
        None     — no nogil scope found (default GIL-held context).
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "with_statement":
            kind = with_statement_kind(cur, source)
            if kind is not None:
                return kind
        elif cur.type == "c_function_definition":
            if cdef_function_is_nogil(cur, source):
                return "nogil"
            return None  # cdef function without nogil → GIL held
        cur = cur.parent
    return None


def candidate_kind(node, source: bytes) -> str | None:
    """If `node` is a Python-touching candidate, return a label; else None."""
    if node.type == "raise_statement":
        return "raise"
    if node.type == "joined_str":
        return "f-string"
    if node.type in COMPREHENSION_TYPES:
        return node.type.replace("_", " ")
    if node.type == "call":
        name = u.get_call_name(node, source)
        if name in PYTHON_BUILTINS:
            return f"call to `{name}`"
    return None


def get_enclosing_function_name(node, source: bytes) -> str | None:
    """Find the name of the enclosing Python or Cython function, if any."""
    fn = u.find_enclosing(node, ["function_definition", "cdef_statement"])
    if fn is None:
        return None
    if fn.type == "function_definition":
        for c in fn.children:
            if c.type == "identifier":
                return u.get_text(c, source)
        return None
    if u.is_cdef_function(fn):
        _ret, name_node, _fn_def = u.get_cdef_function_parts(fn)
        if name_node is not None:
            return u.get_text(name_node, source)
    return None


def analyze_file(path: Path, source: bytes) -> list[dict]:
    tree = u.parse_bytes(source)
    findings: list[dict] = []
    seen: set[tuple[int, int]] = set()  # dedupe nested matches

    for node in u.walk(tree.root_node):
        kind = candidate_kind(node, source)
        if kind is None:
            continue
        scope = determining_gil_scope(node, source)
        if scope != "nogil":
            continue

        key = (node.start_point[0], node.start_point[1])
        if key in seen:
            continue
        seen.add(key)

        function_name = get_enclosing_function_name(node, source)
        line = node.start_point[0] + 1
        column = node.start_point[1] + 1

        # raise statements are the most clearly-bug-shaped; everything else is
        # MEDIUM since some patterns (e.g. `print` in a tightly-controlled
        # debugging build) might be inside a contrived nogil scope.
        if kind == "raise":
            classification = "FIX"
            confidence = "HIGH"
        else:
            classification = "FIX"
            confidence = "MEDIUM"

        findings.append(
            u.make_finding(
                file=path,
                line=line,
                column=column,
                function=function_name,
                category="nogil_python_touch",
                classification=classification,
                confidence=confidence,
                description=(
                    f"{kind.capitalize()} appears inside a `nogil` scope "
                    f"without an enclosing `with gil:` block. Touching Python "
                    f"objects without holding the GIL is undefined behavior."
                ),
                fix_template=(
                    f"Wrap the offending statement in `with gil:`:\n"
                    f"    with gil:\n"
                    f"        # {kind} here\n"
                    f"or move it out of the `nogil` scope entirely."
                ),
                details={
                    "kind": kind,
                    "scope": "nogil",
                },
            )
        )

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
        "script": "scan_cython_nogil_pyobject",
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
