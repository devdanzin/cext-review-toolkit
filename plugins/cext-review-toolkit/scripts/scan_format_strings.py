#!/usr/bin/env python3
"""Validate PyArg_ParseTuple / Py_BuildValue format strings.

Checks that the number of format codes in the format string matches
the number of variadic arguments passed to the call.

Also covers PyErr_Format, PyUnicode_FromFormat (printf-style formats).

Usage:
    python scan_format_strings.py [path] [--max-files N]
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
from scan_common import find_project_root, discover_c_files, parse_common_args


# PyArg_ParseTuple format codes that consume one C argument.
# See https://docs.python.org/3/c-api/arg.html
_PYARG_FORMAT_CODES = set("bBhHiIlLnOfdsUySzZpP")

# Py_BuildValue format codes that consume one C argument.
_BUILD_FORMAT_CODES = set("bBhHiIlLnOfdsUNSzZ")

# Printf-style format codes (for PyErr_Format, PyUnicode_FromFormat).
_PRINTF_FORMAT_RE = re.compile(r"%(?:\d+\$)?[-+ #0]*\d*(?:\.\d+)?(?:l{0,2}|z|j|t|h{0,2})?[diouxXeEfFgGaAcspnR%UV]")

# APIs using PyArg_ParseTuple-style format strings.
_PYARG_APIS = {
    "PyArg_ParseTuple",
    "PyArg_ParseTupleAndKeywords",
    "PyArg_VaParse",
    "PyArg_VaParseTupleAndKeywords",
    "Py_BuildValue",
}

# APIs using printf-style format strings.
_PRINTF_APIS = {
    "PyErr_Format",
    "PyErr_FormatUnraisable",
    "PyUnicode_FromFormat",
    "PyUnicode_FromFormatV",
    "PyErr_SetString",  # Not printf-style, but often confused
}


def _count_pyarg_format_args(fmt: str) -> int | None:
    """Count the number of C arguments expected by a PyArg format string.

    Returns None if the format string can't be parsed (e.g., contains
    unknown format codes or complex nested structures).
    """
    count = 0
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch in _PYARG_FORMAT_CODES:
            count += 1
        elif ch == "(":
            # Tuple: each code inside consumes an arg
            pass
        elif ch == ")":
            pass
        elif ch == "|":
            # Optional arguments separator — doesn't consume an arg
            pass
        elif ch == "$":
            # Keyword-only separator
            pass
        elif ch == ":":
            # Function name follows — stop counting
            break
        elif ch == ";":
            # Error message follows — stop counting
            break
        elif ch == "#":
            # Followed by a Py_ssize_t for string length
            count += 1
        elif ch == "!":
            # Used after 'O' for type checking (O! consumes 2 args)
            count += 1
        elif ch == "&":
            # Used after 'O' for converter (O& consumes 2 args)
            count += 1
        elif ch == "e":
            # Encoded string: 'es', 'et', 'es#', 'et#' consume 2-3 args
            if i + 1 < len(fmt) and fmt[i + 1] in "st":
                count += 2  # encoding + buffer
                i += 1
                if i + 1 < len(fmt) and fmt[i + 1] == "#":
                    count += 1  # + length
                    i += 1
            else:
                return None  # Unknown 'e' usage
        elif ch in " \t\n":
            pass  # Whitespace is allowed
        elif ch == "*":
            # y*, s*, z*, w* — buffer protocol, consumes 1 Py_buffer
            pass  # The preceding format code already counted
        elif ch == "{":
            # Not a standard format code
            return None
        else:
            # Unknown format code
            return None
        i += 1
    return count


def _count_printf_format_args(fmt: str) -> int:
    """Count the number of arguments expected by a printf-style format string."""
    count = 0
    for m in _PRINTF_FORMAT_RE.finditer(fmt):
        spec = m.group()
        if spec == "%%":
            continue  # Literal %
        count += 1
    return count


def _extract_format_string(call_text: str, api_name: str) -> str | None:
    """Extract the format string literal from a call expression text.

    Returns the format string content (without quotes), or None if
    the format string is not a literal.
    """
    # Find the argument that is a string literal.
    # For PyArg_ParseTuple: 2nd arg (after self/args)
    # For Py_BuildValue: 1st arg
    # For PyErr_Format: 2nd arg (after exception type)
    # For PyUnicode_FromFormat: 1st arg

    # Simple approach: find all string literals in the call text
    strings = re.findall(r'"((?:[^"\\]|\\.)*)"', call_text)
    if not strings:
        return None

    if api_name in ("Py_BuildValue", "PyUnicode_FromFormat", "PyUnicode_FromFormatV"):
        # First string literal is the format
        return strings[0] if strings else None
    elif api_name in ("PyArg_ParseTuple", "PyArg_VaParse"):
        # Second arg is format (first is args tuple)
        return strings[0] if strings else None
    elif api_name == "PyArg_ParseTupleAndKeywords":
        # Third arg is format (first is args, second is kwargs)
        return strings[0] if strings else None
    elif api_name in ("PyErr_Format", "PyErr_FormatUnraisable"):
        # Second arg is format (first is exception type)
        return strings[0] if strings else None
    else:
        return strings[0] if strings else None


def _count_variadic_args(arguments_text: str, api_name: str) -> int | None:
    """Count the variadic arguments after the format string.

    Returns None if we can't determine the count (e.g., macro-wrapped).
    """
    # Split by commas at top level (not inside parens/brackets)
    parts = []
    depth = 0
    current = []
    for ch in arguments_text:
        if ch in "(<[":
            depth += 1
            current.append(ch)
        elif ch in ")>]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    # Determine where the format string is and count args after it
    if api_name in ("Py_BuildValue",):
        # Py_BuildValue(format, ...) — format is arg 0
        return len(parts) - 1 if len(parts) > 0 else None
    elif api_name in ("PyArg_ParseTuple", "PyArg_VaParse"):
        # PyArg_ParseTuple(args, format, ...) — format is arg 1
        return len(parts) - 2 if len(parts) > 1 else None
    elif api_name == "PyArg_ParseTupleAndKeywords":
        # PyArg_ParseTupleAndKeywords(args, kwargs, format, kwlist, ...)
        # format is arg 2, kwlist is arg 3, variadics start at arg 4
        return len(parts) - 4 if len(parts) > 3 else None
    elif api_name in ("PyErr_Format", "PyErr_FormatUnraisable"):
        # PyErr_Format(exc, format, ...) — format is arg 1
        return len(parts) - 2 if len(parts) > 1 else None
    elif api_name in ("PyUnicode_FromFormat",):
        # PyUnicode_FromFormat(format, ...) — format is arg 0
        return len(parts) - 1 if len(parts) > 0 else None
    return None


def _check_format_strings(func, source_bytes):
    """Check format string argument counts in a function."""
    findings = []
    body = func["body_node"]

    all_apis = _PYARG_APIS | _PRINTF_APIS
    calls = find_calls_in_scope(body, source_bytes, api_names=all_apis)

    for call in calls:
        api_name = call["function_name"]
        args_text = call["arguments_text"]
        full_text = get_node_text(call["node"], source_bytes)

        fmt_str = _extract_format_string(full_text, api_name)
        if fmt_str is None:
            continue  # Not a literal format string

        if api_name in _PYARG_APIS:
            expected = _count_pyarg_format_args(fmt_str)
            if expected is None:
                continue  # Couldn't parse format
        elif api_name in _PRINTF_APIS:
            expected = _count_printf_format_args(fmt_str)
        else:
            continue

        actual = _count_variadic_args(args_text, api_name)
        if actual is None:
            continue

        if actual != expected:
            findings.append({
                "type": "format_string_mismatch",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "high",
                "detail": (
                    f"{api_name}() format string expects {expected} "
                    f"variadic arg(s) but {actual} provided"
                ),
                "api_call": api_name,
                "format_string": fmt_str,
                "expected_args": expected,
                "actual_args": actual,
            })

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for format string mismatches."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

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
            for f in _check_format_strings(func, source_bytes):
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
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
        "skipped_files": skipped,
    }


def main():
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
