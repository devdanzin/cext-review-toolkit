#!/usr/bin/env python3
"""Find reference counting errors in C extension code.

Detects leaked refs, borrowed-ref-across-callback, stolen-ref misuse,
and missing cleanup on error paths. This analyzes code that *calls* the
Python/C API, not code that *implements* it.

Usage:
    python scan_refcounts.py [path] [--max-files N]
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
)
from scan_common import (
    find_project_root,
    discover_c_files,
    load_api_tables,
    find_assigned_variable,
    parse_common_args,
)


# APIs that could execute arbitrary Python code (may invalidate borrowed refs).
_PYTHON_EXECUTING_APIS = {
    "Py_DECREF",
    "Py_XDECREF",
    "Py_CLEAR",
    "PyObject_SetAttr",
    "PyObject_SetAttrString",
    "PyObject_SetItem",
    "PyObject_DelItem",
    "PyObject_Call",
    "PyObject_CallObject",
    "PyObject_CallFunction",
    "PyObject_CallMethod",
    "PyObject_CallNoArgs",
    "PyObject_CallOneArg",
    "PyObject_RichCompare",
    "PyObject_IsTrue",
    "PyObject_Hash",
    "PyObject_Str",
    "PyObject_Repr",
    "PyObject_Format",
    "PyObject_Bytes",
    "PyObject_ASCII",
    "PyErr_SetObject",
    "PyErr_Format",
    "PyObject_GetAttr",
    "PyObject_GetAttrString",
    "PyObject_GetItem",
}

_DECREF_APIS = {"Py_DECREF", "Py_XDECREF", "Py_CLEAR", "Py_SETREF"}

# Borrowed-ref APIs on immutable containers. Items borrowed from these
# containers are safe across Python calls as long as the container itself
# is alive, because immutable containers hold strong refs to their items
# and no Python call can mutate them.
_IMMUTABLE_CONTAINER_BORROWED_APIS = {
    "PyTuple_GetItem",
    "PyTuple_GET_ITEM",
}


def _var_in_text(var: str, text: str) -> bool:
    """Check if a variable name appears as a word in text."""
    return bool(re.search(r"\b" + re.escape(var) + r"\b", text))


def _check_potential_leaks(func, source_bytes, api_tables):
    """Check for new references that are never released."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])
    steal_apis = set(api_tables["steal_ref_apis"])

    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    returns = find_return_statements(body, source_bytes)
    return_values = {r["value_text"] for r in returns if r["value_text"]}

    decref_calls = find_calls_in_scope(body, source_bytes, api_names=_DECREF_APIS)
    decref_vars = set()
    for dc in decref_calls:
        args = dc["arguments_text"]
        # First arg to Py_DECREF/Py_XDECREF is the variable.
        first_arg = args.split(",")[0].strip()
        decref_vars.add(first_arg)

    steal_calls = find_calls_in_scope(body, source_bytes, api_names=steal_apis)
    stolen_vars = set()
    for sc in steal_calls:
        # The stolen argument varies by API but is usually the last one.
        args = sc["arguments_text"]
        parts = [p.strip() for p in args.split(",")]
        if parts:
            stolen_vars.add(parts[-1])

    # Track new-ref variables.
    for call in all_calls:
        if call["function_name"] not in new_ref_apis:
            continue
        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue
        # Check if variable is handled.
        is_decrefd = var in decref_vars
        is_returned = var in return_values
        is_stolen = var in stolen_vars
        if not (is_decrefd or is_returned or is_stolen):
            findings.append(
                {
                    "type": "potential_leak",
                    "file": "",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "medium",
                    "detail": (
                        f"New reference from {call['function_name']}() "
                        f"assigned to '{var}' may not be released"
                    ),
                    "api_call": call["function_name"],
                    "variable": var,
                }
            )

    return findings


def _check_leak_on_error(func, source_bytes, api_tables):
    """Check for leaks on error paths (return NULL between new-ref and DECREF)."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])

    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    returns = find_return_statements(body, source_bytes)
    error_returns = [r for r in returns if r["value_text"] == "NULL"]

    # For each new-ref assignment, check if there's an error return
    # between it and its DECREF that doesn't clean up the variable.
    new_ref_vars = {}  # var -> (call, start_byte)
    for call in all_calls:
        if call["function_name"] not in new_ref_apis:
            continue
        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue
        new_ref_vars[var] = (call, call["start_byte"])

    decref_calls = find_calls_in_scope(body, source_bytes, api_names=_DECREF_APIS)
    decref_positions = {}
    for dc in decref_calls:
        args = dc["arguments_text"].split(",")[0].strip()
        if args not in decref_positions or dc["start_byte"] < decref_positions[args]:
            decref_positions[args] = dc["start_byte"]

    for var, (call, acq_byte) in new_ref_vars.items():
        decref_byte = decref_positions.get(var)
        if decref_byte is None:
            continue  # Already caught by potential_leak
        for er in error_returns:
            er_byte = er["node"].start_byte
            if acq_byte < er_byte < decref_byte:
                # There's an error return between acquire and DECREF.
                # Check if this error return has a DECREF for our var
                # by looking at the surrounding if-block.
                parent = er["node"].parent
                if parent:
                    block_text = get_node_text(parent, source_bytes)
                    if not _var_in_text(var, block_text) or not any(
                        d in block_text for d in ("Py_DECREF", "Py_XDECREF", "Py_CLEAR")
                    ):
                        findings.append(
                            {
                                "type": "potential_leak_on_error",
                                "file": "",
                                "function": func["name"],
                                "line": er["start_line"],
                                "confidence": "medium",
                                "detail": (
                                    f"Error return at line {er['start_line']} may leak "
                                    f"'{var}' (acquired at line {call['start_line']} "
                                    f"via {call['function_name']}())"
                                ),
                                "api_call": call["function_name"],
                                "variable": var,
                                "error_return_line": er["start_line"],
                                "acquire_line": call["start_line"],
                            }
                        )
    return findings


def _check_borrowed_ref_across_call(func, source_bytes, api_tables):
    """Check for borrowed references used after intervening Python calls."""
    findings = []
    body = func["body_node"]

    borrowed_apis = set(api_tables["borrowed_ref_apis"])
    new_ref_apis = set(api_tables["new_ref_apis"])
    python_executing = new_ref_apis | _PYTHON_EXECUTING_APIS

    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    for i, call in enumerate(all_calls):
        if call["function_name"] not in borrowed_apis:
            continue

        borrowed_var = find_assigned_variable(call["node"], source_bytes)
        if borrowed_var is None:
            continue

        # Suppress borrowed refs from immutable containers (e.g. tuples).
        # The container holds a strong ref to the item, and immutable
        # containers can't have items removed by Python calls.
        if call["function_name"] in _IMMUTABLE_CONTAINER_BORROWED_APIS:
            continue

        # Scan forward for an intervening Python-executing call.
        for j in range(i + 1, len(all_calls)):
            intervening = all_calls[j]
            if intervening["function_name"] not in python_executing:
                continue

            # Check if borrowed_var is used after this intervening call.
            # First: used as argument to another call (high confidence).
            found_in_call = False
            for k in range(j + 1, len(all_calls)):
                later = all_calls[k]
                if _var_in_text(borrowed_var, later["arguments_text"]):
                    findings.append(
                        {
                            "type": "borrowed_ref_across_call",
                            "file": "",
                            "function": func["name"],
                            "line": call["start_line"],
                            "confidence": "high",
                            "detail": (
                                f"Borrowed ref '{borrowed_var}' from "
                                f"{call['function_name']}() used after "
                                f"{intervening['function_name']}() "
                                f"(line {intervening['start_line']}) which could "
                                f"invalidate it"
                            ),
                            "borrowed_api": call["function_name"],
                            "borrowed_var": borrowed_var,
                            "intervening_call": intervening["function_name"],
                            "intervening_line": intervening["start_line"],
                            "use_after_line": later["start_line"],
                        }
                    )
                    found_in_call = True
                    break

            if not found_in_call:
                # Second: used in member access, dereference, or assignment.
                after_bytes = source_bytes[intervening["node"].end_byte : body.end_byte]
                after_text = after_bytes.decode("utf-8", errors="replace")
                esc = re.escape(borrowed_var)
                if (
                    re.search(r"\b" + esc + r"\s*->", after_text)
                    or re.search(r"\*\s*" + esc + r"\b", after_text)
                    or re.search(r"=\s*" + esc + r"\s*;", after_text)
                ):
                    findings.append(
                        {
                            "type": "borrowed_ref_across_call",
                            "file": "",
                            "function": func["name"],
                            "line": call["start_line"],
                            "confidence": "medium",
                            "detail": (
                                f"Borrowed ref '{borrowed_var}' from "
                                f"{call['function_name']}() used after "
                                f"{intervening['function_name']}() "
                                f"(line {intervening['start_line']}) which "
                                f"could invalidate it"
                            ),
                            "borrowed_api": call["function_name"],
                            "borrowed_var": borrowed_var,
                            "intervening_call": intervening["function_name"],
                            "intervening_line": intervening["start_line"],
                        }
                    )

            break  # Only check the first intervening call.

    return findings


def _check_stolen_ref_misuse(func, source_bytes, api_tables):
    """Check for use of a variable after it has been stolen."""
    findings = []
    body = func["body_node"]

    steal_apis = set(api_tables["steal_ref_apis"])
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    for i, call in enumerate(all_calls):
        if call["function_name"] not in steal_apis:
            continue

        args = [a.strip() for a in call["arguments_text"].split(",")]
        if not args:
            continue
        stolen_var = args[-1]
        # Strip casts.
        stolen_var = re.sub(r"\([^)]+\)\s*", "", stolen_var).strip()
        if not re.match(r"^\w+$", stolen_var):
            continue

        # Check if the variable is used after the steal call.
        for j in range(i + 1, len(all_calls)):
            later = all_calls[j]
            if later["function_name"] in _DECREF_APIS and _var_in_text(
                stolen_var, later["arguments_text"]
            ):
                findings.append(
                    {
                        "type": "stolen_ref_not_nulled",
                        "file": "",
                        "function": func["name"],
                        "line": later["start_line"],
                        "confidence": "high",
                        "detail": (
                            f"Variable '{stolen_var}' DECREF'd at line "
                            f"{later['start_line']} after being stolen by "
                            f"{call['function_name']}() at line "
                            f"{call['start_line']}"
                        ),
                        "steal_api": call["function_name"],
                        "variable": stolen_var,
                        "steal_line": call["start_line"],
                    }
                )
                break

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for reference counting errors."""
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
            for checker in (
                _check_potential_leaks,
                _check_leak_on_error,
                _check_borrowed_ref_across_call,
                _check_stolen_ref_misuse,
            ):
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
