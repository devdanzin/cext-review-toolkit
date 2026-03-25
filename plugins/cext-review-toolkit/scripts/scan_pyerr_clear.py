#!/usr/bin/env python3
"""Audit PyErr_Clear() calls for exception swallowing in C extension code.

Finds PyErr_Clear() calls that are not preceded by PyErr_ExceptionMatches()
or similar type-checking calls. Unguarded clears silently swallow MemoryError,
KeyboardInterrupt, and SystemExit — a common and dangerous anti-pattern.

Usage:
    python scan_pyerr_clear.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes_for_file, extract_functions,
    get_node_text, walk_descendants, strip_comments,
)
from scan_common import find_project_root, discover_c_files, parse_common_args


# APIs that check the exception type before clearing.
_EXCEPTION_CHECK_APIS = {
    "PyErr_ExceptionMatches",
    "PyErr_GivenExceptionMatches",
}

# APIs that fetch/inspect the current exception (weaker guard).
_EXCEPTION_FETCH_APIS = {
    "PyErr_Fetch",
    "PyErr_GetRaisedException",
    "PyErr_GetHandledException",
}

# Known hot-path function name patterns (heuristic).
_HOT_PATH_PATTERNS = re.compile(
    r'(?:get|set|next|iter|contains|subscript|length|item|repr|str|hash|call)',
    re.IGNORECASE,
)


def _find_pyerr_clear_calls(func: dict, source_bytes: bytes) -> list[dict]:
    """Find all PyErr_Clear() calls in a function body."""
    calls = []
    body_node = func["body_node"]
    for node in walk_descendants(body_node, type_filter="call_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node and get_node_text(fn_node, source_bytes) == "PyErr_Clear":
            calls.append({
                "node": node,
                "line": node.start_point[0] + 1,
            })
    return calls


def _has_exception_check_before(clear_node, func: dict,
                                 source_bytes: bytes) -> bool:
    """Check if there's a PyErr_ExceptionMatches() guarding a PyErr_Clear().

    Walks up from the clear call to find an enclosing if/else that checks
    the exception type. Also checks for PyErr_Fetch/GetRaisedException
    in the same scope (weaker guard but still intentional).
    """
    body_text = func["body"]
    clear_offset = clear_node.start_byte - func["body_node"].start_byte

    # Check the preceding context (up to 500 chars before the clear).
    start = max(0, clear_offset - 500)
    preceding = body_text[start:clear_offset]

    # Strong guard: ExceptionMatches check before clear.
    for api in _EXCEPTION_CHECK_APIS:
        if api in preceding:
            return True

    # Medium guard: exception fetch before clear (intentional handling).
    for api in _EXCEPTION_FETCH_APIS:
        if api in preceding:
            return True

    # Walk up the AST to check enclosing if conditions.
    node = clear_node.parent
    while node and node != func["body_node"]:
        if node.type in ("if_statement", "else_clause"):
            # Check the condition of the if statement.
            if node.type == "if_statement":
                cond = node.child_by_field_name("condition")
                if cond:
                    cond_text = get_node_text(cond, source_bytes)
                    for api in _EXCEPTION_CHECK_APIS:
                        if api in cond_text:
                            return True
            # Check if this else belongs to an if with ExceptionMatches.
            if node.type == "else_clause":
                parent_if = node.parent
                if parent_if and parent_if.type == "if_statement":
                    cond = parent_if.child_by_field_name("condition")
                    if cond:
                        cond_text = get_node_text(cond, source_bytes)
                        for api in _EXCEPTION_CHECK_APIS:
                            if api in cond_text:
                                return True
        node = node.parent

    return False


def _is_in_hot_path(func_name: str) -> bool:
    """Heuristic: check if function name suggests a hot path."""
    return bool(_HOT_PATH_PATTERNS.search(func_name))


def _check_pyerr_clear(func: dict, source_bytes: bytes) -> list[dict]:
    """Check a function for unguarded PyErr_Clear() calls."""
    findings = []
    clear_calls = _find_pyerr_clear_calls(func, source_bytes)

    for call in clear_calls:
        if _has_exception_check_before(call["node"], func, source_bytes):
            continue

        is_hot = _is_in_hot_path(func["name"])
        finding_type = ("broad_pyerr_clear_in_hot_path"
                        if is_hot else "unguarded_pyerr_clear")
        confidence = "high" if is_hot else "medium"

        findings.append({
            "type": finding_type,
            "file": "",
            "function": func["name"],
            "line": call["line"],
            "confidence": confidence,
            "detail": (
                f"PyErr_Clear() in '{func['name']}' without "
                f"PyErr_ExceptionMatches() guard — silently swallows "
                f"MemoryError, KeyboardInterrupt, SystemExit"
            ),
        })

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for unguarded PyErr_Clear() calls."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    findings: list[dict] = []
    total_functions = 0
    total_clears = 0
    files_analyzed = 0
    skipped: list[dict] = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        for func in functions:
            total_functions += 1
            clear_calls = _find_pyerr_clear_calls(func, source_bytes)
            total_clears += len(clear_calls)

            for f in _check_pyerr_clear(func, source_bytes):
                f["file"] = rel
                findings.append(f)

    by_type = defaultdict(int)
    by_confidence = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_confidence[f["confidence"]] += 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "functions_analyzed": total_functions,
        "files_analyzed": files_analyzed,
        "total_pyerr_clear_calls": total_clears,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
        "skipped_files": skipped,
    }


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
