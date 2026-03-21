#!/usr/bin/env python3
"""Measure complexity metrics for C functions using Tree-sitter.

Adapted from cpython-review-toolkit's version, using Tree-sitter for
function detection instead of regex. The complexity metrics themselves
(cyclomatic complexity, nesting depth, line count, parameter count,
goto count) stay the same.

Outputs a JSON structure with per-function metrics:
- line_count, nesting_depth, cyclomatic_complexity
- parameter_count, local_variable_count, goto_count, switch_case_count
- weighted score (1-10)

Usage:
    python measure_c_complexity.py [path]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from pathlib import Path

# Import shared utilities.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import parse_bytes_for_file, extract_functions, get_node_text, walk_descendants, strip_comments
from scan_common import find_project_root, discover_c_files, parse_common_args


# ---------------------------------------------------------------------------
# Complexity metrics (same logic as cpython-review-toolkit)
# ---------------------------------------------------------------------------

_BRANCH_KEYWORDS = re.compile(
    r'\b(if|else\s+if|case|for|while|do)\b'
)
_LOGICAL_OPS = re.compile(r'(&&|\|\|)')
_TERNARY = re.compile(r'\?')
_GOTO = re.compile(r'\bgoto\b')
_SWITCH_CASE = re.compile(r'\bcase\b')
_LOCAL_VAR = re.compile(
    r'^\s+(?:(?:static|const|volatile|unsigned|signed|long|short|register)\s+)*'
    r'(?:int|char|float|double|void|Py_ssize_t|size_t|PyObject|'
    r'Py_hash_t|Py_uhash_t|uint\d+_t|int\d+_t|long|short|unsigned)\s*\*?\s+\w+',
    re.MULTILINE,
)


def _strip_comments_and_strings(source: str) -> str:
    """Remove C comments and string/char literals from source."""
    source = re.sub(r'/\*.*?\*/', ' ', source, flags=re.DOTALL)
    source = re.sub(r'//[^\n]*', ' ', source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


def measure_function(func: dict) -> dict:
    """Compute complexity metrics for a single C function."""
    body = func["body"]
    clean = _strip_comments_and_strings(body)
    body_lines = [line for line in clean.split('\n') if line.strip()]
    line_count = len(body_lines)

    # Parameter count.
    params = func["parameters"].strip()
    if params and params != "void":
        param_count = params.count(',') + 1
    else:
        param_count = 0

    # Cyclomatic complexity: branches + logical ops + ternary + 1.
    branches = len(_BRANCH_KEYWORDS.findall(clean))
    logical = len(_LOGICAL_OPS.findall(clean))
    ternary = len(_TERNARY.findall(clean))
    cyclomatic = branches + logical + ternary + 1

    # Nesting depth.
    max_depth = 0
    depth = 0
    for ch in clean:
        if ch == '{':
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == '}':
            depth = max(0, depth - 1)

    # Goto count.
    goto_count = len(_GOTO.findall(clean))

    # Switch-case count.
    switch_case_count = len(_SWITCH_CASE.findall(clean))

    # Local variable count.
    local_var_count = len(_LOCAL_VAR.findall(clean))

    # Weighted score (1-10).
    score = 1.0
    if line_count > 200:
        score += min((line_count - 200) / 100, 3.0)
    elif line_count > 100:
        score += (line_count - 100) / 100 * 1.5
    elif line_count > 50:
        score += (line_count - 50) / 100

    if max_depth > 5:
        score += min((max_depth - 5) * 0.5, 2.0)
    elif max_depth > 3:
        score += (max_depth - 3) * 0.25

    if cyclomatic > 20:
        score += min((cyclomatic - 20) / 10, 2.5)
    elif cyclomatic > 10:
        score += (cyclomatic - 10) / 20

    if param_count > 6:
        score += min((param_count - 6) * 0.3, 1.0)

    if goto_count > 5:
        score += min((goto_count - 5) * 0.2, 0.5)

    score = min(max(round(score, 1), 1.0), 10.0)

    return {
        "name": func["name"],
        "start_line": func["start_line"],
        "end_line": func["end_line"],
        "line_count": line_count,
        "nesting_depth": max_depth,
        "cyclomatic_complexity": cyclomatic,
        "parameter_count": param_count,
        "local_variable_count": local_var_count,
        "goto_count": goto_count,
        "switch_case_count": switch_case_count,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C function complexity for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    files_data: list[dict] = []
    total_functions = 0
    hotspot_count = 0
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

        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        file_entry: dict = {"file": rel, "functions": []}

        for func in functions:
            metrics = measure_function(func)
            total_functions += 1
            if metrics["score"] >= 5.0:
                hotspot_count += 1
            file_entry["functions"].append(metrics)

        if file_entry["functions"]:
            files_data.append(file_entry)

    # Collect all functions and sort by score descending.
    all_funcs = []
    for f in files_data:
        for fn in f["functions"]:
            all_funcs.append({**fn, "file": f["file"]})
    all_funcs.sort(key=lambda x: -x["score"])

    result = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "functions_analyzed": total_functions,
        "files": files_data,
        "hotspots": all_funcs[:30],
        "summary": {
            "total_functions": total_functions,
            "hotspot_count": hotspot_count,
            "avg_cyclomatic": (
                round(
                    sum(fn["cyclomatic_complexity"] for fn in all_funcs)
                    / max(len(all_funcs), 1),
                    1,
                )
            ),
            "avg_line_count": (
                round(
                    sum(fn["line_count"] for fn in all_funcs)
                    / max(len(all_funcs), 1),
                    1,
                )
            ),
            "max_nesting": max(
                (fn["nesting_depth"] for fn in all_funcs), default=0
            ),
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
