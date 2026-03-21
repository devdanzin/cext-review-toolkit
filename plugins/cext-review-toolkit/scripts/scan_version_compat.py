#!/usr/bin/env python3
"""Scan C extension code for version compatibility issues.

Checks API usage against known deprecated and removed APIs,
detects missing version guards, and finds dead compatibility code.

Usage:
    python scan_version_compat.py [path] [--max-files N] [--min-python 3.9]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes, extract_functions, find_calls_in_scope,
    get_node_text, strip_comments,
)
from scan_common import find_project_root, discover_c_files

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_deprecated_apis() -> dict:
    """Load deprecated/removed API data."""
    try:
        with open(_DATA_DIR / "deprecated_apis.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Failed to load deprecated_apis.json: {e}"}))
        sys.exit(1)


def _version_tuple(ver: str) -> tuple[int, int]:
    """Parse '3.10' into (3, 10)."""
    parts = ver.split(".")
    return (int(parts[0]), int(parts[1]))


def _version_hex(ver: str) -> int:
    """Parse '3.10' into 0x030A0000."""
    major, minor = _version_tuple(ver)
    return (major << 24) | (minor << 16)


def _check_removed_api(functions, source_bytes, api_data, min_python):
    """Find calls to removed APIs."""
    findings = []
    min_ver = _version_tuple(min_python)
    removed_apis = {}
    for entry in api_data["deprecated"]:
        removed_in = entry.get("removed_in")
        if removed_in and _version_tuple(removed_in) <= min_ver:
            removed_apis[entry["name"]] = entry

    if not removed_apis:
        return findings

    api_names = set(removed_apis.keys())
    for func in functions:
        calls = find_calls_in_scope(func["body_node"], source_bytes,
                                     api_names=api_names)
        for call in calls:
            entry = removed_apis[call["function_name"]]
            # Check if the call is inside a version guard.
            body_text = func["body"]
            call_offset = call["start_byte"] - func["body_node"].start_byte
            before_text = body_text[:call_offset]
            # Simple check: is there a #if PY_VERSION_HEX guard?
            if re.search(r'#\s*if\s+PY_VERSION_HEX', before_text):
                continue

            findings.append({
                "type": "removed_api_usage",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "high",
                "detail": (f"{call['function_name']}() removed in Python "
                           f"{entry['removed_in']}. "
                           f"Replacement: {entry.get('replacement', 'none')}"),
                "api": call["function_name"],
                "removed_in": entry["removed_in"],
                "replacement": entry.get("replacement"),
            })

    return findings


def _check_deprecated_api(functions, source_bytes, api_data):
    """Find calls to deprecated APIs."""
    findings = []
    deprecated_apis = {}
    for entry in api_data["deprecated"]:
        if entry.get("replacement"):
            deprecated_apis[entry["name"]] = entry

    if not deprecated_apis:
        return findings

    api_names = set(deprecated_apis.keys())
    for func in functions:
        calls = find_calls_in_scope(func["body_node"], source_bytes,
                                     api_names=api_names)
        for call in calls:
            entry = deprecated_apis[call["function_name"]]
            findings.append({
                "type": "deprecated_api_usage",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "medium",
                "detail": (f"{call['function_name']}() deprecated since Python "
                           f"{entry['deprecated_since']}. "
                           f"Use {entry['replacement']} instead. "
                           f"{entry.get('note', '')}"),
                "api": call["function_name"],
                "deprecated_since": entry["deprecated_since"],
                "replacement": entry.get("replacement"),
            })

    return findings


def _check_missing_version_guard(functions, source_bytes, api_data, min_python):
    """Find newer API usage without version guards."""
    findings = []
    min_ver = _version_tuple(min_python)
    version_added = api_data.get("version_added", {})

    # Only flag APIs added after the minimum version.
    newer_apis = {}
    for api, ver in version_added.items():
        if _version_tuple(ver) > min_ver:
            newer_apis[api] = ver

    if not newer_apis:
        return findings

    api_names = set(newer_apis.keys())
    for func in functions:
        calls = find_calls_in_scope(func["body_node"], source_bytes,
                                     api_names=api_names)
        for call in calls:
            ver = newer_apis[call["function_name"]]
            # Check for version guard.
            body_text = func["body"]
            call_offset = call["start_byte"] - func["body_node"].start_byte
            before_text = body_text[:call_offset]
            if re.search(r'#\s*if\s+PY_VERSION_HEX', before_text):
                continue

            findings.append({
                "type": "missing_version_guard",
                "file": "",
                "function": func["name"],
                "line": call["start_line"],
                "confidence": "high",
                "detail": (f"{call['function_name']}() requires Python {ver}+ "
                           f"but min version is {min_python} — needs "
                           f"#if PY_VERSION_HEX >= 0x{_version_hex(ver):08X} guard"),
                "api": call["function_name"],
                "added_in": ver,
                "min_python": min_python,
            })

    return findings


def _check_dead_version_guards(source_text, min_python):
    """Find #if PY_VERSION_HEX < ... guards below minimum Python."""
    findings = []
    min_hex = _version_hex(min_python)

    pattern = re.compile(
        r'#\s*if\s+PY_VERSION_HEX\s*<\s*(0x[0-9A-Fa-f]+)')
    for m in pattern.finditer(source_text):
        guard_hex = int(m.group(1), 16)
        if guard_hex <= min_hex:
            line = source_text[:m.start()].count('\n') + 1
            # Decode the version from hex.
            major = (guard_hex >> 24) & 0xFF
            minor = (guard_hex >> 16) & 0xFF
            findings.append({
                "type": "dead_version_guard",
                "file": "",
                "function": "(preprocessor)",
                "line": line,
                "confidence": "high",
                "detail": (f"Version guard for Python < {major}.{minor} "
                           f"is dead code — minimum supported is {min_python}"),
                "guard_version": f"{major}.{minor}",
                "guard_hex": m.group(1),
                "min_python": min_python,
            })

    return findings


def _detect_min_python(project_root: Path) -> str | None:
    """Try to detect minimum Python version from project config."""
    for config_file, pattern in [
        ("setup.py", r'python_requires\s*=\s*["\']>=?\s*(\d+\.\d+)'),
        ("pyproject.toml", r'requires-python\s*=\s*">=?\s*(\d+\.\d+)'),
        ("setup.cfg", r'python_requires\s*=\s*>=?\s*(\d+\.\d+)'),
    ]:
        path = project_root / config_file
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                m = re.search(pattern, content)
                if m:
                    return m.group(1)
            except OSError:
                continue
    return None


def analyze(target: str, *, max_files: int = 0,
            min_python: str = "3.9") -> dict:
    """Scan C files for version compatibility issues."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    api_data = _load_deprecated_apis()

    # Try to detect min_python from project config.
    detected = _detect_min_python(project_root)
    if detected:
        min_python = detected

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
        source_text = source_bytes.decode("utf-8", errors="replace")

        if not functions:
            # Still check for dead version guards even without functions.
            files_analyzed += 1
            try:
                rel = str(filepath.relative_to(project_root))
            except ValueError:
                rel = str(filepath)
            for f in _check_dead_version_guards(source_text, min_python):
                f["file"] = rel
                findings.append(f)
            continue

        files_analyzed += 1
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        total_functions += len(functions)

        for checker_result in [
            _check_removed_api(functions, source_bytes, api_data, min_python),
            _check_deprecated_api(functions, source_bytes, api_data),
            _check_missing_version_guard(functions, source_bytes,
                                          api_data, min_python),
            _check_dead_version_guards(source_text, min_python),
        ]:
            for f in checker_result:
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
        "min_python": min_python,
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
        min_python = "3.9"
        positional: list[str] = []
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                max_files = int(argv[i + 1])
                i += 2
            elif argv[i] == "--min-python" and i + 1 < len(argv):
                min_python = argv[i + 1]
                i += 2
            elif argv[i].startswith("--"):
                i += 1
            else:
                positional.append(argv[i])
                i += 1
        target = positional[0] if positional else "."
        result = analyze(target, max_files=max_files, min_python=min_python)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
