#!/usr/bin/env python3
"""scan_cython_pycapsule.py — Query 3: detect `PyCapsule_New(ptr, name, NULL)`
calls (NULL destructor).

A PyCapsule with a NULL destructor performs no cleanup when the capsule itself
is garbage-collected — the wrapped pointer is the caller's responsibility.
This is correct ONLY when:

  (a) ownership is transferred to a downstream wrapper that will free it,
      AND no other wrapper aliases the same pointer; OR
  (b) the wrapped resource has a separate lifecycle managed elsewhere.

In practice, NULL-destructor capsules combined with multi-wrap (e.g.,
`as_ffi_ptr()` returning a capsule that `array_from_ffi_ptr()` consumes
without ownership transfer) produce double-free / use-after-free at
process exit. This was F11 in the blosc2 review (32 sites identified by
the refcount informed agent; the highest-impact two pair were
`as_ffi_ptr` + `array_from_ffi_ptr` which produced live-reproducible
ASAN double-free).

The scanner reports candidates; the agent triages whether each site has
proper ownership transfer.

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

CAPSULE_NEW = "PyCapsule_New"

# Pattern for the second argument: a string literal containing the capsule name
# Examples: "blosc2_schunk*", b"array_t*"  (b-prefix is fine)
NAME_LITERAL_REGEX = re.compile(r'^\s*[bu]?(?:"([^"]*)"|\'([^\']*)\')')


def is_null_literal(arg_text: str) -> bool:
    """True if the third argument to PyCapsule_New is the NULL literal.

    Matches: NULL, <void*> NULL, NULL_DESTRUCTOR (any all-caps NULL identifier
    used as a placeholder by some codebases). Conservative — only matches
    very specific spellings. False negatives (a non-NULL but inert destructor)
    are out of scope here; false positives would be misleading.
    """
    cleaned = arg_text.strip()
    # Strip cast syntax like `<void*> NULL`
    cleaned = re.sub(r"<[^>]+>\s*", "", cleaned).strip()
    return cleaned == "NULL"


def extract_capsule_name(arg_text: str) -> str | None:
    """Extract the capsule-name string literal from the second argument.

    The capsule name in real Cython code is a `<char *> "blosc2_schunk*"` or
    similar — a string literal possibly with a cast. We tolerate the cast and
    the b-prefix.
    """
    cleaned = arg_text.strip()
    # Strip casts: `<char *> "name"` → `"name"`
    cleaned = re.sub(r"^<[^>]+>\s*", "", cleaned).strip()
    m = NAME_LITERAL_REGEX.match(cleaned)
    if m:
        return m.group(1) or m.group(2)
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

    for call_node in u.find_nodes(tree.root_node, "call"):
        if u.get_call_name(call_node, source) != CAPSULE_NEW:
            continue

        args = u.get_call_arguments(call_node)
        if len(args) < 3:
            continue  # malformed — skip

        # PyCapsule_New(ptr, name, destructor)
        third_arg_text = u.get_text(args[2], source)
        if not is_null_literal(third_arg_text):
            continue  # has a non-NULL destructor — not our concern

        capsule_name = extract_capsule_name(u.get_text(args[1], source))
        function_name = get_enclosing_function_name(call_node, source)

        findings.append(
            u.make_finding(
                file=path,
                line=call_node.start_point[0] + 1,
                column=call_node.start_point[1] + 1,
                function=function_name,
                category="pycapsule_null_destructor",
                classification="CONSIDER",
                confidence="MEDIUM",
                description=(
                    f"`PyCapsule_New(..., {capsule_name!r}, NULL)` — capsule with "
                    f"NULL destructor. The wrapped pointer's lifecycle must be "
                    f"managed by ownership-transfer to a downstream wrapper, or "
                    f"by a separate cleanup path. Multi-wrap of the same "
                    f"pointer with NULL-destructor capsules can produce "
                    f"double-free at GC time (cf. blosc2 F11: as_ffi_ptr / "
                    f"array_from_ffi_ptr live-reproducible ASAN crash)."
                ),
                fix_template=(
                    f"Either: (a) make the capsule a unique-ownership transfer "
                    f"by NULLing self.<ptr_field> after the capsule is created, "
                    f"OR (b) use a non-NULL destructor that frees the wrapped "
                    f"pointer, OR (c) document the ownership rule explicitly "
                    f"and audit every consumer."
                ),
                details={
                    "capsule_name": capsule_name,
                    "third_arg_text": third_arg_text.strip(),
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
        "script": "scan_cython_pycapsule",
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
