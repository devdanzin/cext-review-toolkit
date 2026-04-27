#!/usr/bin/env python3
"""scan_cython_cdef_int_except.py — Query 1: detect `cdef int` declarations
missing `except`/`except *`/`except -1`/`noexcept` clause.

Cython 3 implicitly applies `noexcept` to `cdef int` (and other integer-typed)
functions without an explicit `except` clause. When such functions are
registered as C callbacks (especially with C-Blosc2, libuv, custom event
loops, etc.), user-raised Python exceptions get silently swallowed at the
C-callback boundary — the function returns -1 with the error indicator set,
but the caller has no way to detect the failure unless the contract requires
a return-value check.

This was the root cause of Cluster C in the blosc2 review (15 callbacks
without `except -1`, with the highest-priority 4 sites at lines 3158, 3180,
3235, 3252 explicitly raising RuntimeError on null-`sc` branches that get
silently swallowed).

Two-layer detection (per Phase 0 calibration learning): even with the fixed
parser, we cross-check candidates against a regex on the source line that
contains the function signature. This guards against future grammar
regressions where `except` clauses might not be properly captured.

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

# Integer-typed return types we care about. These are the Cython types where
# the silent-noexcept rule matters because callers/callbacks check return values.
INT_RETURN_TYPES = frozenset({
    "int",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "long", "long_long", "long long",
    "short", "char",
    "Py_ssize_t",
    "ssize_t", "size_t",
    "bint",  # Cython boolean — same concern (silent return -1)
})

# Regex check applied to the source line containing the function signature.
# Confirms that the line really does/does not contain an `except` token.
EXCEPT_REGEX = re.compile(r"\bexcept\b")
NOEXCEPT_REGEX = re.compile(r"\bnoexcept\b")


def is_int_return(return_type_text: str) -> bool:
    """True if the return type is a Cython integer type used as a value return.

    Returns False for pointer returns (`int*`, `char**`, etc.) because their
    exception semantics differ — pointer returns can use `except NULL` to
    signal failure, but the silent-noexcept rule applies differently. We
    keep this query focused on *numeric* return types where -1/0/etc. are
    used as the failure signal.
    """
    # Pointer returns are out of scope for this query
    if "*" in return_type_text:
        return False
    cleaned = return_type_text.strip()
    if cleaned in INT_RETURN_TYPES:
        return True
    # Also catch unsigned/signed prefixes
    cleaned_no_modifier = re.sub(r"^(unsigned|signed)\s+", "", cleaned).strip()
    return cleaned_no_modifier in INT_RETURN_TYPES


def analyze_file(path: Path, source: bytes) -> list[dict]:
    """Return a list of findings for this file."""
    tree = u.parse_bytes(source)
    findings: list[dict] = []

    for cdef_stmt in u.find_nodes(tree.root_node, "cdef_statement"):
        if not u.is_cdef_function(cdef_stmt):
            continue

        ret_type_node, name_node, fn_def = u.get_cdef_function_parts(cdef_stmt)
        if fn_def is None or name_node is None:
            continue

        return_type_text = u.get_cdef_function_return_text(cdef_stmt, source)
        if not is_int_return(return_type_text):
            continue

        # Layer 1: AST signal — does the function definition have an exception_value?
        ast_has_except = u.has_exception_value(fn_def)
        ast_has_noexcept = u.has_noexcept(fn_def, source)

        # Layer 2: regex check on the function signature line(s).
        # Extract the signature (everything up to the colon that opens the body).
        sig_text = u.get_text(fn_def, source)
        colon_idx = sig_text.find(":")
        sig_only = sig_text[:colon_idx] if colon_idx >= 0 else sig_text

        regex_has_except = bool(EXCEPT_REGEX.search(sig_only))
        regex_has_noexcept = bool(NOEXCEPT_REGEX.search(sig_only))

        has_except = ast_has_except or regex_has_except
        has_noexcept = ast_has_noexcept or regex_has_noexcept

        if has_except or has_noexcept:
            continue

        # Found a candidate
        function_name = u.get_text(name_node, source)
        line = name_node.start_point[0] + 1  # 1-indexed

        findings.append(
            u.make_finding(
                file=path,
                line=line,
                column=name_node.start_point[1] + 1,
                function=function_name,
                category="cdef_int_no_except",
                classification="FIX",
                confidence="HIGH",
                description=(
                    f"`cdef {return_type_text} {function_name}(...)` lacks `except`, "
                    "`except *`, `except -1`, or `noexcept` clause. Cython 3 "
                    "implicitly applies `noexcept` — Python exceptions raised "
                    "inside this function will be silently swallowed at the "
                    "C-callback boundary."
                ),
                fix_template=(
                    f"Add `except -1` (or `except *` for void-conceptual returns, "
                    f"or `except? -1` if -1 is a valid result) to the signature: "
                    f"`cdef {return_type_text} {function_name}(...) except -1:`"
                ),
                details={
                    "return_type": return_type_text,
                    "ast_layer": {
                        "has_except": ast_has_except,
                        "has_noexcept": ast_has_noexcept,
                    },
                    "regex_layer": {
                        "has_except": regex_has_except,
                        "has_noexcept": regex_has_noexcept,
                    },
                },
            )
        )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Main entry point — same calling convention as existing scan scripts."""
    files = u.find_pyx_files(target, max_files=max_files)
    all_findings: list[dict] = []
    parse_errors = 0

    for path in files:
        source = path.read_bytes()
        try:
            findings = analyze_file(path, source)
            all_findings.extend(findings)
        except Exception as e:  # don't let one bad file kill the whole scan
            parse_errors += 1
            print(f"WARNING: failed to analyze {path}: {e}", file=sys.stderr)

    return {
        "script": "scan_cython_cdef_int_except",
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
    ap.add_argument("--max-files", type=int, default=0, help="max files to scan (0 = unlimited)")
    args = ap.parse_args()
    result = analyze(args.target, max_files=args.max_files)
    json.dump(result, sys.stdout, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
