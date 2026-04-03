#!/usr/bin/env python3
"""Find NULL safety issues in C extension code.

Detects unchecked allocations, dereference-before-check, and unchecked
PyArg_Parse calls.

Usage:
    python scan_null_checks.py [path] [--max-files N]
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
    get_node_text,
)
from scan_common import (
    find_project_root,
    discover_c_files,
    load_api_tables,
    find_assigned_variable,
    PYARG_PARSE_APIS,
    parse_common_args,
)


# Macros that dereference their argument — crash if NULL.
_DEREF_MACROS = {
    "PyBytes_AS_STRING",
    "PyBytes_GET_SIZE",
    "PyByteArray_AS_STRING",
    "PyByteArray_GET_SIZE",
    "PyList_GET_ITEM",
    "PyList_GET_SIZE",
    "PyList_SET_ITEM",
    "PyTuple_GET_ITEM",
    "PyTuple_GET_SIZE",
    "PyTuple_SET_ITEM",
    "PyUnicode_GET_LENGTH",
    "PyUnicode_READ_CHAR",
    "PyUnicode_DATA",
    "PyUnicode_READ",
    "PyFloat_AS_DOUBLE",
    "PySequence_Fast_GET_ITEM",
    "PySequence_Fast_GET_SIZE",
    "PyWeakref_GET_OBJECT",
    "PyCell_GET",
    "PyCell_SET",
    "Py_SIZE",
    "Py_TYPE",
    "Py_REFCNT",
}

# APIs that return NULL on encoding/conversion failure.
_NULLABLE_CONVERSION_APIS = {
    "PyUnicode_AsASCIIString",
    "PyUnicode_AsUTF8String",
    "PyUnicode_AsEncodedString",
    "PyUnicode_AsUTF8AndSize",
    "PyObject_GetAttr",
    "PyObject_GetAttrString",
}

_ALLOC_APIS = {
    "PyMem_Malloc",
    "PyMem_Calloc",
    "PyMem_Realloc",
    "PyObject_Malloc",
    "PyObject_Calloc",
    "PyObject_Realloc",
    "PyMem_New",
    "PyMem_Resize",
    "malloc",
    "calloc",
    "realloc",
}

# APIs that return borrowed refs (NULL means not found, not always error).
_BORROWED_NULL_APIS = {
    "PyDict_GetItem",
    "PyDict_GetItemString",
    "PyDict_GetItemWithError",
}

# APIs that handle NULL arguments safely (no dereference on NULL input).
# Passing NULL to these is not a bug — they check internally.
_NULL_SAFE_APIS = {
    "Py_XDECREF",
    "Py_XINCREF",
    "Py_CLEAR",
    "PyMem_Free",
    "PyMem_RawFree",
    "PyObject_Free",
    "PyBuffer_Release",
    "PyObject_InitVar",  # Checks ob != NULL (CPython Objects/object.c:542)
    "free",
}


def _has_null_check_after(var: str, after_text: str) -> bool:
    """Check if variable has a NULL check in the text following its assignment.

    Also recognizes Cython-generated patterns:
    - if (unlikely(!var)) __PYX_ERR(...)
    - if (unlikely(var == ((type)NULL)))
    - if (!var) __PYX_ERR(...)
    """
    return bool(
        re.search(
            r"if\s*\(\s*" + re.escape(var) + r"\s*==\s*NULL|"
            r"if\s*\(\s*!\s*" + re.escape(var) + r"\b|"
            r"if\s*\(\s*" + re.escape(var) + r"\s*!=\s*NULL|"
            r"if\s*\(\s*" + re.escape(var) + r"\s*\)|"
            # Cython: if (unlikely(!var)) or if (unlikely(var == ...NULL...))
            r"if\s*\(\s*unlikely\s*\(\s*!" + re.escape(var) + r"\b|"
            r"if\s*\(\s*unlikely\s*\(\s*" + re.escape(var) + r"\s*==|"
            # __PYX_ERR as error handler (implies a preceding NULL check)
            r"if\s*\([^)]*" + re.escape(var) + r"[^)]*\)\s*\{?\s*__PYX_ERR",
            after_text,
        )
    )


def _check_unchecked_alloc(func, source_bytes, api_tables):
    """Find allocation calls whose result is not checked for NULL."""
    findings = []
    body = func["body_node"]
    body_text = get_node_text(body, source_bytes)

    alloc_calls = find_calls_in_scope(body, source_bytes, api_names=_ALLOC_APIS)
    for call in alloc_calls:
        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue

        after_text = body_text[call["node"].end_byte - body.start_byte :]
        if _has_null_check_after(var, after_text):
            continue

        # Check if it's a direct return (return malloc(...)) -- not checkable.
        parent = call["node"].parent
        if parent and parent.type == "return_statement":
            continue

        findings.append(
            {
                "type": "unchecked_alloc",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "high",
                "detail": (
                    f"Return value of {call['function_name']}() "
                    f"assigned to '{var}' without NULL check"
                ),
                "api_call": call["function_name"],
                "variable": var,
            }
        )

    return findings


def _check_deref_before_check(func, source_bytes, api_tables):
    """Find pointer dereferences before NULL check."""
    findings = []
    body = func["body_node"]
    body_text = get_node_text(body, source_bytes)

    new_ref_apis = set(api_tables["new_ref_apis"])
    nullable_apis = new_ref_apis | _ALLOC_APIS | _BORROWED_NULL_APIS

    nullable_calls = find_calls_in_scope(body, source_bytes, api_names=nullable_apis)
    for call in nullable_calls:
        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue

        after_text = body_text[call["node"].end_byte - body.start_byte :]

        # Look for dereference (var->, *var) before NULL check.
        deref_match = re.search(re.escape(var) + r"\s*->", after_text)
        null_check_match = re.search(
            r"if\s*\(\s*(?:!"
            + re.escape(var)
            + r"|"
            + re.escape(var)
            + r"\s*==\s*NULL)",
            after_text,
        )

        if deref_match and (
            not null_check_match or deref_match.start() < null_check_match.start()
        ):
            deref_line = call["start_line"] + after_text[: deref_match.start()].count(
                "\n"
            )
            findings.append(
                {
                    "type": "deref_before_check",
                    "file": "",
                    "function": func["name"],
                    "line": deref_line,
                    "confidence": "medium",
                    "detail": (
                        f"Pointer '{var}' (from {call['function_name']}()) "
                        f"dereferenced before NULL check"
                    ),
                    "api_call": call["function_name"],
                    "variable": var,
                }
            )

    return findings


def _var_in_text(var: str, text: str) -> bool:
    """Check if a variable name appears as a word in text."""
    return bool(re.search(r"\b" + re.escape(var) + r"\b", text))


def _check_deref_macro_on_unchecked(func, source_bytes, api_tables):
    """Find dereference-like macros called on potentially-NULL values."""
    findings = []
    body = func["body_node"]
    new_ref_apis = set(api_tables["new_ref_apis"])
    nullable_apis = new_ref_apis | _NULLABLE_CONVERSION_APIS | _BORROWED_NULL_APIS

    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda c: c["start_byte"])

    # Track variables assigned from nullable APIs.
    nullable_vars: dict[str, tuple[str, int, int]] = {}
    for call in all_calls:
        if call["function_name"] not in nullable_apis:
            continue
        var = find_assigned_variable(call["node"], source_bytes)
        if var:
            nullable_vars[var] = (
                call["function_name"],
                call["start_line"],
                call["start_byte"],
            )

    # Check if any deref macro is called with a nullable var
    # without an intervening NULL check.
    body_text = get_node_text(body, source_bytes)

    for call in all_calls:
        # Skip null-safe APIs that handle NULL arguments internally.
        if call["function_name"] in _NULL_SAFE_APIS:
            continue
        if call["function_name"] not in _DEREF_MACROS:
            continue

        args_text = call["arguments_text"]
        for var, (api, api_line, api_byte) in nullable_vars.items():
            if not _var_in_text(var, args_text):
                continue

            between_text = body_text[
                api_byte - body.start_byte : call["start_byte"] - body.start_byte
            ]
            has_null_check = bool(
                re.search(
                    r"\bif\s*\(\s*" + re.escape(var) + r"\s*==\s*NULL|"
                    r"if\s*\(\s*!\s*" + re.escape(var) + r"\b|"
                    r"if\s*\(\s*" + re.escape(var) + r"\s*!=\s*NULL",
                    between_text,
                )
            )

            if not has_null_check:
                findings.append(
                    {
                        "type": "deref_macro_on_unchecked",
                        "file": "",
                        "function": func["name"],
                        "line": call["start_line"],
                        "confidence": "high",
                        "detail": (
                            f"{call['function_name']}({var}) at line "
                            f"{call['start_line']} — '{var}' from "
                            f"{api}() (line {api_line}) may be NULL"
                        ),
                        "macro": call["function_name"],
                        "variable": var,
                        "source_api": api,
                        "source_line": api_line,
                    }
                )

    return findings


def _check_unchecked_pyarg_parse(func, source_bytes, api_tables):
    """Find unchecked PyArg_ParseTuple calls."""
    findings = []
    body = func["body_node"]

    parse_calls = find_calls_in_scope(body, source_bytes, api_names=PYARG_PARSE_APIS)
    for call in parse_calls:
        checked = False
        node = call["node"].parent
        while node and node != body:
            if node.type in (
                "if_statement",
                "parenthesized_expression",
                "unary_expression",
                "binary_expression",
            ):
                checked = True
                break
            if node.type == "expression_statement":
                break
            node = node.parent

        if not checked:
            findings.append(
                {
                    "type": "unchecked_pyarg_parse",
                    "file": "",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "high",
                    "detail": f"{call['function_name']}() return value not checked",
                    "api_call": call["function_name"],
                }
            )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for NULL safety issues."""
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
                _check_unchecked_alloc,
                _check_deref_before_check,
                _check_deref_macro_on_unchecked,
                _check_unchecked_pyarg_parse,
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
