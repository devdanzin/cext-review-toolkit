#!/usr/bin/env python3
"""Find error handling bugs in C extension code.

Detects missing NULL checks, return-without-exception, exception clobbering,
and unchecked PyArg_Parse calls.

Usage:
    python scan_error_paths.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes, extract_functions, find_calls_in_scope,
    find_return_statements, get_node_text, walk_descendants,
    get_declarator_name,
)
from scan_common import (
    find_project_root, discover_c_files, load_api_tables,
    find_assigned_variable, PYARG_PARSE_APIS,
)


_PYERR_SET_APIS = {
    "PyErr_SetString", "PyErr_Format", "PyErr_SetObject",
    "PyErr_NoMemory", "PyErr_BadArgument", "PyErr_SetNone",
    "PyErr_BadInternalCall", "PyErr_SetFromErrno",
    "PyErr_SetFromErrnoWithFilename",
    "PyErr_SetFromWindowsErr",
}

_CLEANUP_APIS = {
    "Py_DECREF", "Py_XDECREF", "Py_CLEAR", "Py_SETREF",
    "PyMem_Free", "PyObject_Free", "free",
}


def _check_missing_null_check(func, source_bytes, api_tables):
    """Find new-ref API calls whose result is used without NULL check."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])
    body_text = get_node_text(body, source_bytes)

    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    for call in all_calls:
        if call["function_name"] not in new_ref_apis:
            continue

        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue

        # Check if the variable is NULL-checked before next significant use.
        # Look for if (var == NULL), if (!var), if (var) in the body text
        # after the assignment.
        after_text = body_text[call["node"].end_byte - body.start_byte:]
        has_null_check = bool(re.search(
            r'\b(?:if\s*\(\s*' + re.escape(var) + r'\s*==\s*NULL|'
            r'if\s*\(\s*!\s*' + re.escape(var) + r'\b|'
            r'if\s*\(\s*' + re.escape(var) + r'\s*(?:!=\s*NULL|==\s*NULL))',
            after_text
        ))

        if not has_null_check:
            # Check if it's used as a direct return (return func()) -- that's fine.
            parent = call["node"].parent
            if parent and parent.type == "return_statement":
                continue
            # Also check if it's inside a conditional already.
            gparent = parent.parent if parent else None
            if gparent and gparent.type in ("if_statement", "conditional_expression"):
                continue

            findings.append({
                "type": "missing_null_check",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "medium",
                "detail": (f"Return value of {call['function_name']}() "
                           f"assigned to '{var}' without NULL check"),
                "api_call": call["function_name"],
                "variable": var,
            })

    return findings


def _check_return_without_exception(func, source_bytes, api_tables):
    """Find error returns (NULL/-1) without a preceding PyErr_Set* call."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])

    # Check if this function ever returns PyObject* (NULL return is error).
    returns_pyobject = "PyObject" in func["return_type"]
    returns_int = func["return_type"].strip() in ("int", "static int")

    if not (returns_pyobject or returns_int):
        return findings

    returns = find_return_statements(body, source_bytes)
    pyerr_calls = find_calls_in_scope(body, source_bytes, api_names=_PYERR_SET_APIS)
    all_calls = find_calls_in_scope(body, source_bytes)

    error_value = "NULL" if returns_pyobject else "-1"

    for ret in returns:
        if ret["value_text"] != error_value:
            continue

        ret_byte = ret["node"].start_byte

        # Check if there's a PyErr_* call before this return.
        has_err_set = False
        for ec in pyerr_calls:
            if ec["start_byte"] < ret_byte:
                has_err_set = True
                break

        if has_err_set:
            continue

        # Check if a preceding API call sets the exception on failure
        # (new-ref APIs set exception before returning NULL).
        has_api_err = False
        for ac in all_calls:
            if ac["start_byte"] >= ret_byte:
                break
            if ac["function_name"] in new_ref_apis or \
               ac["function_name"] in PYARG_PARSE_APIS:
                has_api_err = True
                break

        if has_api_err:
            continue

        findings.append({
            "type": "return_without_exception",
            "file": "",
            "function": func["name"],
            "line": ret["start_line"],
            "confidence": "medium",
            "detail": (f"Returns {error_value} at line {ret['start_line']} "
                       f"without a preceding PyErr_Set* call"),
        })

    return findings


def _check_exception_clobbering(func, source_bytes, api_tables):
    """Find places where a pending exception may be clobbered."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])

    # Look for if-blocks that check for NULL (error detection),
    # then call non-cleanup Python APIs before returning.
    for if_node in walk_descendants(body, "if_statement"):
        cond = if_node.child_by_field_name("condition")
        if not cond:
            continue
        cond_text = get_node_text(cond, source_bytes)

        # Check if condition tests for error (== NULL, < 0).
        is_error_check = bool(re.search(
            r'==\s*NULL|<\s*0|!\s*\w', cond_text
        ))
        if not is_error_check:
            continue

        # Get the consequence block.
        consequence = if_node.child_by_field_name("consequence")
        if not consequence:
            continue

        # Check for non-cleanup API calls in the error block.
        calls_in_block = find_calls_in_scope(consequence, source_bytes)
        for call in calls_in_block:
            fn = call["function_name"]
            if fn in _CLEANUP_APIS:
                continue
            if fn.startswith("Py") and fn not in _CLEANUP_APIS:
                # Non-cleanup Python API in error path.
                findings.append({
                    "type": "exception_clobbering",
                    "file": "",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "medium",
                    "detail": (f"Call to {fn}() in error handling block "
                               f"(line {if_node.start_point[0] + 1}) could "
                               f"clobber the pending exception"),
                    "api_call": fn,
                    "error_check_line": if_node.start_point[0] + 1,
                })

    return findings


def _check_unchecked_pyarg_parse(func, source_bytes, api_tables):
    """Find unchecked PyArg_ParseTuple calls."""
    findings = []
    body = func["body_node"]

    parse_calls = find_calls_in_scope(body, source_bytes, api_names=PYARG_PARSE_APIS)
    for call in parse_calls:
        # Check if the call's return value is tested.
        # Common patterns: if (!PyArg_Parse...) or result = PyArg_Parse...
        # If the call is inside an if-condition or negation, it's checked.
        checked = False
        node = call["node"].parent
        while node and node != body:
            if node.type in ("if_statement", "parenthesized_expression",
                             "unary_expression", "binary_expression"):
                checked = True
                break
            if node.type in ("expression_statement",):
                # Bare call -- not checked.
                break
            node = node.parent

        if not checked:
            findings.append({
                "type": "unchecked_pyarg_parse",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "high",
                "detail": (f"{call['function_name']}() return value not checked"),
                "api_call": call["function_name"],
            })

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for error handling bugs."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    api_tables = load_api_tables()
    findings = []
    total_functions = 0
    files_analyzed = 0
    skipped = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        tree = parse_bytes(source_bytes)
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
            for checker in (_check_missing_null_check,
                            _check_return_without_exception,
                            _check_exception_clobbering,
                            _check_unchecked_pyarg_parse):
                for f in checker(func, source_bytes, api_tables):
                    f["file"] = rel
                    findings.append(f)

    by_type = defaultdict(int)
    by_confidence = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_confidence[f["confidence"]] += 1

    result = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "functions_analyzed": total_functions,
        "files_analyzed": files_analyzed,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
    }
    result["skipped_files"] = skipped
    return result


def main() -> None:
    try:
        max_files = 0
        positional: list[str] = []
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                max_files = int(argv[i + 1])
                i += 2
            elif argv[i].startswith("--"):
                i += 1
            else:
                positional.append(argv[i])
                i += 1
        target = positional[0] if positional else "."
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
