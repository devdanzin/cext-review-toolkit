#!/usr/bin/env python3
"""Track non-PyObject resource allocation/free pairing in C extension code.

Finds allocations (malloc, PyMem_Malloc, H5Tcreate, PyObject_GetBuffer, etc.)
that may not have a matching free on all exit paths. This catches resource leaks
on error paths — the most impactful bug class found in h5py, scipy, and pandas.

Usage:
    python scan_resource_lifecycle.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes_for_file,
    extract_functions,
    find_calls_in_scope,
    find_return_statements,
    get_node_text,
    walk_descendants,
    strip_comments,
)
from scan_common import (
    find_project_root,
    discover_c_files,
    find_assigned_variable,
    parse_common_args,
)


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_resource_pairs() -> dict[str, list[str]]:
    """Load resource allocation/free pairs from data file.

    Returns a dict mapping alloc function name -> list of valid free functions.
    """
    pairs_file = _DATA_DIR / "resource_pairs.json"
    try:
        with open(pairs_file, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: Failed to load {pairs_file}: {e}", file=sys.stderr)
        print(
            json.dumps({"error": f"Failed to load resource_pairs.json: {e}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    alloc_to_free: dict[str, list[str]] = {}
    for pair in data.get("pairs", []):
        free_funcs = pair.get("free", [])
        for alloc_func in pair.get("alloc", []):
            alloc_to_free[alloc_func] = free_funcs
    return alloc_to_free


def _find_allocations(
    func: dict, source_bytes: bytes, alloc_to_free: dict[str, list[str]]
) -> list[dict]:
    """Find resource allocation calls in a function and their assigned variables."""
    allocations = []
    body = func["body_node"]
    alloc_names = set(alloc_to_free.keys())

    all_calls = find_calls_in_scope(body, source_bytes, api_names=alloc_names)
    for call in all_calls:
        var_name = find_assigned_variable(call["node"], source_bytes)
        if not var_name:
            continue

        alloc_func = call["function_name"]
        free_funcs = alloc_to_free.get(alloc_func, [])

        allocations.append(
            {
                "variable": var_name,
                "alloc_func": alloc_func,
                "free_funcs": free_funcs,
                "line": call["start_line"],
                "call_node": call["node"],
            }
        )

    return allocations


def _find_free_calls(
    func: dict, source_bytes: bytes, free_funcs: set[str]
) -> list[dict]:
    """Find all free/release/close calls in a function."""
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes, api_names=free_funcs)
    result = []
    for call in all_calls:
        args_text = call.get("arguments_text", "")
        result.append(
            {
                "function_name": call["function_name"],
                "arguments_text": args_text,
                "line": call["start_line"],
            }
        )
    return result


def _find_goto_labels(func: dict, source_bytes: bytes) -> dict[str, int]:
    """Find all goto labels in a function and their line numbers."""
    labels = {}
    body = func["body_node"]
    for node in walk_descendants(body, "labeled_statement"):
        label_node = node.child_by_field_name("label")
        if label_node:
            label_name = get_node_text(label_node, source_bytes)
            labels[label_name] = node.start_point[0] + 1
    return labels


def _find_exit_points(func: dict, source_bytes: bytes) -> list[dict]:
    """Find all function exit points (return statements and goto error labels)."""
    exits = []
    body = func["body_node"]

    # Return statements.
    returns = find_return_statements(body, source_bytes)
    for ret in returns:
        value = ret.get("value_text") or ""
        exits.append(
            {
                "type": "return",
                "line": ret["start_line"],
                "value": value,
                "is_error": _is_error_return(value),
            }
        )

    return exits


def _is_error_return(value: str) -> bool:
    """Check if a return value indicates an error (NULL, -1, etc.)."""
    v = value.strip()
    return v in ("NULL", "-1", "0") or v == ""


def _variable_freed_in_text(var_name: str, free_funcs: list[str], text: str) -> bool:
    """Check if a variable is freed in a given text span."""
    for free_func in free_funcs:
        # Match free_func(var) or free_func(&var) or free_func(self->var)
        pattern = rf"{re.escape(free_func)}\s*\([^)]*\b{re.escape(var_name)}\b"
        if re.search(pattern, text):
            return True
    return False


def _check_resource_lifecycle(
    func: dict, source_bytes: bytes, alloc_to_free: dict[str, list[str]]
) -> list[dict]:
    """Check that all allocated resources are freed on all exit paths."""
    findings = []
    allocations = _find_allocations(func, source_bytes, alloc_to_free)

    if not allocations:
        return findings

    body_text = strip_comments(func["body"])
    exits = _find_exit_points(func, source_bytes)

    # Collect all free function names for lookup.
    all_free_funcs: set[str] = set()
    for alloc in allocations:
        all_free_funcs.update(alloc["free_funcs"])

    for alloc in allocations:
        var = alloc["variable"]
        free_funcs = alloc["free_funcs"]
        alloc_line = alloc["line"]

        if not free_funcs:
            continue

        # Check: is the variable freed at all in the function?
        freed_anywhere = _variable_freed_in_text(var, free_funcs, body_text)

        if not freed_anywhere:
            # Resource never freed in this function — might be stored or returned.
            # Check if variable is returned or stored in a struct.
            if _is_returned_or_stored(var, body_text):
                continue

            findings.append(
                {
                    "type": "resource_never_freed",
                    "file": "",
                    "function": func["name"],
                    "line": alloc_line,
                    "confidence": "high",
                    "detail": (
                        f"Resource '{var}' allocated by {alloc['alloc_func']}() "
                        f"is never freed by {'/'.join(free_funcs)}() "
                        f"in function '{func['name']}'"
                    ),
                    "variable": var,
                    "alloc_func": alloc["alloc_func"],
                    "expected_free": free_funcs,
                }
            )
            continue

        # Resource IS freed somewhere — check if it's freed on error paths too.
        # Look at each exit point: is there a free before it?
        for exit_point in exits:
            if not exit_point["is_error"]:
                continue

            exit_line = exit_point["line"]
            if exit_line <= alloc_line:
                continue  # Exit is before the allocation.

            # Get the text between allocation and this exit.
            alloc_offset = _line_to_offset(body_text, alloc_line - func["start_line"])
            exit_offset = _line_to_offset(body_text, exit_line - func["start_line"])

            if alloc_offset is None or exit_offset is None:
                continue

            span = body_text[alloc_offset:exit_offset]

            # Skip error returns that are the NULL check for this allocation
            # itself (the resource doesn't exist on this path).
            if _is_null_check_for_alloc(
                var, alloc_line, exit_line, body_text, func["start_line"]
            ):
                continue

            # Check if the variable is freed in this span.
            if not _variable_freed_in_text(var, free_funcs, span):
                # Also check if there's a goto to a cleanup label that frees it.
                if _has_goto_cleanup_freeing(var, free_funcs, span, body_text):
                    continue

                findings.append(
                    {
                        "type": "resource_leak_on_error_path",
                        "file": "",
                        "function": func["name"],
                        "line": exit_line,
                        "confidence": "medium",
                        "detail": (
                            f"Resource '{var}' (allocated at line {alloc_line} "
                            f"by {alloc['alloc_func']}()) may not be freed "
                            f"before error return at line {exit_line}"
                        ),
                        "variable": var,
                        "alloc_func": alloc["alloc_func"],
                        "alloc_line": alloc_line,
                        "expected_free": free_funcs,
                    }
                )

    return findings


def _line_to_offset(text: str, line_delta: int) -> int | None:
    """Convert a line delta to a byte offset in text."""
    offset = 0
    for _ in range(line_delta):
        idx = text.find("\n", offset)
        if idx == -1:
            return None
        offset = idx + 1
    return offset


def _is_returned_or_stored(var: str, body_text: str) -> bool:
    """Check if a variable is returned directly or stored in a struct member."""
    # Check for direct return: return var; or return (type*)var;
    # But NOT return func(var) — that's passing as argument, not returning.
    if re.search(rf"\breturn\s+(?:\([^)]*\)\s*)?{re.escape(var)}\s*;", body_text):
        return True
    # Check for struct member assignment: self->field = var or obj.field = var.
    if re.search(rf"->\w+\s*=\s*(?:\([^)]*\)\s*)?{re.escape(var)}\s*;", body_text):
        return True
    if re.search(rf"\.\w+\s*=\s*(?:\([^)]*\)\s*)?{re.escape(var)}\s*;", body_text):
        return True
    return False


def _is_null_check_for_alloc(
    var: str, alloc_line: int, exit_line: int, full_body: str, func_start_line: int
) -> bool:
    """Check if the error return is inside a NULL check for the allocated variable.

    E.g., the error return in: if (buf == NULL) { return NULL; }
    The resource doesn't exist here, so this isn't a leak.

    The check must be within 3 lines after the allocation for it to be
    considered the allocation's own null check (not a later check).
    """
    # Get the text from 1 line before alloc to 4 lines after alloc.
    # The if-check typically follows on the next line after the allocation.
    check_start = max(0, alloc_line - func_start_line - 1)
    check_end = min(alloc_line - func_start_line + 4, full_body.count("\n") + 1)

    start_offset = _line_to_offset(full_body, check_start)
    end_offset = _line_to_offset(full_body, check_end)
    if start_offset is None:
        start_offset = 0
    if end_offset is None:
        end_offset = len(full_body)

    context = full_body[start_offset:end_offset]

    # Only suppress if the error return is within this narrow window.
    if not (alloc_line <= exit_line <= alloc_line + 4):
        return False

    patterns = [
        rf"\bif\s*\(\s*!\s*{re.escape(var)}\s*\)",
        rf"\bif\s*\(\s*{re.escape(var)}\s*==\s*NULL\s*\)",
        rf"\bif\s*\(\s*{re.escape(var)}\s*==\s*0\s*\)",
        rf"\bif\s*\(\s*{re.escape(var)}\s*<\s*0\s*\)",
    ]
    return any(re.search(p, context) for p in patterns)


def _has_goto_cleanup_freeing(
    var: str, free_funcs: list[str], span: str, full_body: str
) -> bool:
    """Check if the span contains a goto to a cleanup label that frees var."""
    goto_matches = re.finditer(r"\bgoto\s+(\w+)", span)
    for m in goto_matches:
        label = m.group(1)
        # Find text after the label in the full body.
        label_pattern = rf"\b{re.escape(label)}\s*:"
        label_match = re.search(label_pattern, full_body)
        if label_match:
            cleanup_text = full_body[label_match.end() :]
            if _variable_freed_in_text(var, free_funcs, cleanup_text):
                return True
    return False


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for resource lifecycle issues."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    alloc_to_free = _load_resource_pairs()

    findings: list[dict] = []
    total_functions = 0
    total_allocations = 0
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
            allocs = _find_allocations(func, source_bytes, alloc_to_free)
            total_allocations += len(allocs)

            for f in _check_resource_lifecycle(func, source_bytes, alloc_to_free):
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
        "total_tracked_allocations": total_allocations,
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
