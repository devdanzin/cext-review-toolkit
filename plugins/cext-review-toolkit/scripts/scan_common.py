#!/usr/bin/env python3
"""Shared utilities for cext-review-toolkit analysis scripts.

Provides common infrastructure: project root detection, C file discovery,
API table loading, and AST helpers used across multiple scanner scripts.
"""

import json
import sys
from collections.abc import Generator
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_c
except ImportError:
    print(json.dumps({
        "error": "tree-sitter not installed",
        "install": "pip install tree-sitter tree-sitter-c",
    }))
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    get_node_text, get_declarator_name,
    C_EXTENSIONS, ALL_SOURCE_EXTENSIONS, is_cpp_available,
)


EXCLUDE_DIRS = frozenset({
    ".git", ".tox", ".venv", "venv", "__pycache__",
    "node_modules", "build", "dist", ".eggs", "egg-info",
})

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PYARG_PARSE_APIS = frozenset({
    "PyArg_ParseTuple", "PyArg_ParseTupleAndKeywords",
    "PyArg_Parse", "PyArg_UnpackTuple",
    "PyArg_VaParse", "PyArg_VaParseTupleAndKeywords",
})


def find_project_root(start: Path) -> Path:
    """Find project root by looking for common project markers."""
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        for marker in (".git", "pyproject.toml", "setup.py", "setup.cfg"):
            if (current / marker).exists():
                return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


def _get_source_extensions() -> frozenset[str]:
    """Return file extensions to scan (C only, or C+C++ if available)."""
    return ALL_SOURCE_EXTENSIONS if is_cpp_available() else C_EXTENSIONS


def discover_c_files(
    root: Path, *, max_files: int = 0,
) -> Generator[Path, None, None]:
    """Discover C/C++ source files under root, excluding common build dirs."""
    exts = _get_source_extensions()
    count = 0
    if root.is_file():
        if root.suffix in exts:
            yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in exts:
            continue
        try:
            parts = set(p.relative_to(root).parts)
        except ValueError:
            continue
        if parts & EXCLUDE_DIRS:
            continue
        yield p
        count += 1
        if max_files and count >= max_files:
            return


def load_api_tables() -> dict:
    """Load API classification tables from the data directory."""
    try:
        with open(_DATA_DIR / "api_tables.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Failed to load api_tables.json: {e}"}))
        sys.exit(1)


def find_assigned_variable(call_node, source_bytes: bytes) -> str | None:
    """Find the variable a call result is assigned to."""
    node = call_node.parent
    while node:
        if node.type == "init_declarator":
            decl = node.child_by_field_name("declarator")
            if decl:
                return get_declarator_name(decl, source_bytes)
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left:
                return get_node_text(left, source_bytes)
        # Skip past macro wrappers (ALL_CAPS function calls that wrap assignments)
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                func_text = get_node_text(func, source_bytes)
                if func_text.isupper():
                    node = node.parent
                    continue
        if node.type in ("expression_statement", "declaration",
                          "compound_statement"):
            break
        node = node.parent
    return None


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Deduplicate findings by (type, file, normalized detail).

    Groups findings that have the same type and file with similar
    detail text (after stripping line numbers and variable names).
    Keeps the first occurrence as canonical and adds duplicate_count
    and duplicate_locations fields.
    """
    import re as _re

    def _normalize_detail(detail: str) -> str:
        """Strip line numbers and variable names for grouping."""
        text = _re.sub(r"line \d+", "line N", detail)
        text = _re.sub(r"'[^']+?'", "'VAR'", text)
        return text

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for f in findings:
        key = (f.get("type", ""), f.get("file", ""), _normalize_detail(f.get("detail", "")))
        groups.setdefault(key, []).append(f)

    result = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            canonical["duplicate_count"] = len(group) - 1
            canonical["duplicate_locations"] = [
                {"file": d.get("file", ""), "line": d.get("line", 0)}
                for d in group[1:]
            ]
        result.append(canonical)
    return result


def extract_nearby_comments(
    source_bytes: bytes, tree, line: int, radius: int = 5
) -> list[str]:
    """Extract comments within ±radius lines of the given line.

    Uses Tree-sitter to find comment nodes. Returns list of comment
    text strings (stripped of comment markers).
    """
    comments = []
    min_line = max(0, line - radius - 1)  # 0-indexed
    max_line = line + radius - 1

    def _walk(node):
        if node.type == "comment":
            node_line = node.start_point[0]
            if min_line <= node_line <= max_line:
                text = source_bytes[node.start_byte:node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                comments.append(text)
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return comments


_SAFETY_KEYWORDS = {
    "safety:", "safe because", "intentional", "by design",
    "cext-safe:", "nolint", "checked:", "correct because",
    "this is safe", "not a bug", "deliberately", "expected",
}


def has_safety_annotation(comments: list[str]) -> bool:
    """Check if any comment contains a safety annotation keyword."""
    for comment in comments:
        lower = comment.lower()
        if any(kw in lower for kw in _SAFETY_KEYWORDS):
            return True
    return False


def parse_common_args(argv: list[str]) -> tuple[str, int]:
    """Parse common CLI arguments (path and --max-files).

    Returns (target_path, max_files).
    """
    max_files = 0
    positional: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                print(json.dumps({"error": f"--max-files requires an integer, got '{argv[i + 1]}'"}))
                sys.exit(2)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    return target, max_files
