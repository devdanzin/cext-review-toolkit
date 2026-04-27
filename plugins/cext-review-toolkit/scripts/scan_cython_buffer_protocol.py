#!/usr/bin/env python3
"""scan_cython_buffer_protocol.py — Query 2: detect `PyObject_GetBuffer`
calls not paired with `PyBuffer_Release` in an enclosing `try/finally`.

The Python C API's buffer protocol requires that every successful
`PyObject_GetBuffer(obj, &view, ...)` be matched by a corresponding
`PyBuffer_Release(&view)`. Inside a Cython `def`/`cpdef` function, if the
function can `raise` between Get and Release (which most functions can),
the Release must live in a `finally:` block to guarantee execution.

This was Cluster B in the blosc2 review (15 leak sites). The reference correct
pattern in blosc2 is `vlcompress` (lines 1289-1355) which uses
`try: ... finally: PyBuffer_Release(&buf)`. The remaining ~14 buffer-acquiring
entry points predate that template.

Calling convention matches existing scripts:
    analyze(target: str, *, max_files: int = 0) -> dict
    JSON output to stdout via main()
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cython_ast_utils as u

GET_BUFFER = "PyObject_GetBuffer"
RELEASE = "PyBuffer_Release"

# Match `&buffer_var` or `<...>&buffer_var` to extract the buffer variable name
# from the Get/Release call's first/second argument.
BUFFER_VAR_REGEX = re.compile(r"&\s*(\w+)")


def extract_buffer_var(call_node, source: bytes) -> str | None:
    """For a PyObject_GetBuffer or PyBuffer_Release call, extract the buffer
    variable name from the `&view` argument.

    PyObject_GetBuffer(obj, &view, flags) → "view"
    PyBuffer_Release(&view)               → "view"
    """
    args = u.get_call_arguments(call_node)
    # Buffer var is the first &<name> argument (in Get, it's args[1]; in Release, it's args[0])
    for arg in args:
        text = u.get_text(arg, source)
        m = BUFFER_VAR_REGEX.search(text)
        if m:
            return m.group(1)
    return None


def find_release_in_finally(
    enclosing_fn,
    buffer_var: str,
    source: bytes,
) -> bool:
    """True if any `finally_clause` within `enclosing_fn` contains a
    `PyBuffer_Release(&<buffer_var>)` call.
    """
    if enclosing_fn is None:
        return False

    for finally_clause in u.find_nodes(enclosing_fn, "finally_clause"):
        for call_node in u.find_nodes(finally_clause, "call"):
            if u.get_call_name(call_node, source) != RELEASE:
                continue
            release_var = extract_buffer_var(call_node, source)
            if release_var == buffer_var:
                return True
    return False


def find_release_anywhere(
    enclosing_fn,
    buffer_var: str,
    source: bytes,
) -> bool:
    """True if there's ANY PyBuffer_Release(&buffer_var) somewhere in the
    enclosing function (whether or not it's in a try/finally). Used to
    classify findings into "totally missing release" (HIGH) vs "release
    exists but not in finally" (MEDIUM).
    """
    if enclosing_fn is None:
        return False
    for call_node in u.find_nodes(enclosing_fn, "call"):
        if u.get_call_name(call_node, source) != RELEASE:
            continue
        if extract_buffer_var(call_node, source) == buffer_var:
            return True
    return False


def get_enclosing_function_name(node, source: bytes) -> str | None:
    """Find the name of the enclosing Python or Cython function, if any."""
    fn = u.find_enclosing(node, ["function_definition", "cdef_statement"])
    if fn is None:
        return None
    if fn.type == "function_definition":
        # def name(...)
        for c in fn.children:
            if c.type == "identifier":
                return u.get_text(c, source)
        return None
    # cdef_statement → function name via maybe_typed_name
    if u.is_cdef_function(fn):
        _ret, name_node, _fn_def = u.get_cdef_function_parts(fn)
        if name_node is not None:
            return u.get_text(name_node, source)
    return None


def analyze_file(path: Path, source: bytes) -> list[dict]:
    tree = u.parse_bytes(source)
    findings: list[dict] = []

    for call_node in u.find_nodes(tree.root_node, "call"):
        if u.get_call_name(call_node, source) != GET_BUFFER:
            continue

        buffer_var = extract_buffer_var(call_node, source)
        if buffer_var is None:
            continue  # malformed call — skip silently

        enclosing = u.find_enclosing(
            call_node, ["function_definition", "cdef_statement"]
        )
        if enclosing is None:
            continue

        # Two questions:
        # 1) Is there a Release in a finally clause? (the safe pattern)
        # 2) Is there a Release anywhere? (less safe but mitigation present)
        in_finally = find_release_in_finally(enclosing, buffer_var, source)
        if in_finally:
            continue  # safe — skip

        any_release = find_release_anywhere(enclosing, buffer_var, source)
        function_name = get_enclosing_function_name(call_node, source)

        if any_release:
            classification = "FIX"
            confidence = "MEDIUM"
            desc_suffix = (
                f"`PyBuffer_Release(&{buffer_var})` exists in this function but "
                f"NOT inside a `finally:` block — if the code between "
                f"`PyObject_GetBuffer` and `PyBuffer_Release` raises, the buffer "
                f"will not be released."
            )
        else:
            classification = "FIX"
            confidence = "HIGH"
            desc_suffix = (
                f"No `PyBuffer_Release(&{buffer_var})` found anywhere in this "
                f"function — every successful `PyObject_GetBuffer` requires a "
                f"matching `PyBuffer_Release` to avoid leaking the buffer "
                f"export."
            )

        findings.append(
            u.make_finding(
                file=path,
                line=call_node.start_point[0] + 1,
                column=call_node.start_point[1] + 1,
                function=function_name,
                category="buffer_protocol_leak",
                classification=classification,
                confidence=confidence,
                description=(
                    f"`PyObject_GetBuffer(..., &{buffer_var}, ...)` not paired "
                    f"with a `PyBuffer_Release(&{buffer_var})` in `finally:`. "
                    f"{desc_suffix}"
                ),
                fix_template=(
                    f"Wrap the buffer-using code in `try: ... finally: "
                    f"PyBuffer_Release(&{buffer_var})`. "
                    f"Reference template: `vlcompress` in blosc2_ext.pyx."
                ),
                details={
                    "buffer_var": buffer_var,
                    "any_release_present": any_release,
                    "in_finally": False,
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
        "script": "scan_cython_buffer_protocol",
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
