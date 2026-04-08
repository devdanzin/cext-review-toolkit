#!/usr/bin/env python3
"""Analyze git history for churn metrics, commit classification, and co-change data.

Adapted from code-review-toolkit's version. Uses Tree-sitter in addition to
Python AST for C files.

Usage:
    python analyze_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 90)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits
    --max-commits N   Cap total commits analyzed (default: 2000)
    --no-function     Skip function-level churn (file-level only, faster)
"""

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import parse_bytes_for_file, extract_functions
from scan_common import find_project_root

CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    (
        "fix",
        [
            "fix",
            "bug",
            "patch",
            "resolve",
            "issue",
            "crash",
            "error",
            "broken",
            "repair",
            "correct",
            "regression",
            "workaround",
            "hotfix",
            "segfault",
            "leak",
            "null",
            "refcount",
            "decref",
        ],
    ),
    (
        "docs",
        ["doc", "readme", "comment", "typo", "spelling", "changelog", "documentation"],
    ),
    ("test", ["test", "coverage", "assert", "mock", "fixture"]),
    (
        "refactor",
        [
            "refactor",
            "clean",
            "simplify",
            "reorganize",
            "restructure",
            "rename",
            "move",
            "extract",
            "deduplicate",
            "inline",
        ],
    ),
    (
        "chore",
        [
            "bump",
            "dependency",
            "update",
            "upgrade",
            "ci",
            "config",
            "lint",
            "format",
            "version",
            "release",
            "merge",
            "revert",
        ],
    ),
    (
        "feature",
        [
            "add",
            "implement",
            "new",
            "feature",
            "introduce",
            "support",
            "enable",
            "create",
        ],
    ),
]

_GIT_TIMEOUT = 30
_SCRIPT_START: float = 0.0
_SCRIPT_TIMEOUT = 300
_MAX_DIFF_LINES_FIX = 150
_MAX_DIFF_LINES_REFACTOR = 80


def classify_commit(message: str) -> str:
    msg_lower = message.lower()
    for category, keywords in CLASSIFICATION_RULES:
        for keyword in keywords:
            if keyword in msg_lower:
                return category
    return "unknown"


def _run_git(args, cwd, timeout=_GIT_TIMEOUT):
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
    )


def _run_git_streaming(args, cwd):
    return subprocess.Popen(
        ["git"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], path, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_script_timeout() -> bool:
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


def _get_file_line_count(filepath: Path) -> int:
    try:
        return len(filepath.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def parse_git_log(lines, max_commits, project_root=None):
    commits = []
    file_changes = {}
    current_commit = None
    commit_count = 0

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("COMMIT:"):
            if current_commit is not None:
                commits.append(current_commit)
            commit_count += 1
            if commit_count > max_commits:
                break
            parts = line[7:].split("|", 3)
            if len(parts) < 4:
                current_commit = None
                continue
            commit_hash, date_str, author, message = parts
            current_commit = {
                "hash": commit_hash,
                "date": date_str,
                "author": author,
                "message": message,
                "type": classify_commit(message),
                "files": [],
                "stats": [],
            }
        elif line.strip() and current_commit is not None:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added_str, removed_str, filepath = parts
                try:
                    added = int(added_str) if added_str != "-" else 0
                    removed = int(removed_str) if removed_str != "-" else 0
                except ValueError:
                    continue
                current_commit["files"].append(filepath)
                current_commit["stats"].append(
                    {
                        "file": filepath,
                        "added": added,
                        "removed": removed,
                    }
                )
                if filepath not in file_changes:
                    file_changes[filepath] = {
                        "commits": 0,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "authors": set(),
                        "first_date": date_str,
                        "last_date": date_str,
                    }
                fc = file_changes[filepath]
                fc["commits"] += 1
                fc["lines_added"] += added
                fc["lines_removed"] += removed
                fc["authors"].add(author)
                if date_str < fc["first_date"]:
                    fc["first_date"] = date_str
                if date_str > fc["last_date"]:
                    fc["last_date"] = date_str

    if current_commit is not None and commit_count <= max_commits:
        commits.append(current_commit)

    file_stats = []
    for filepath, fc in file_changes.items():
        line_count = (
            _get_file_line_count(project_root / filepath) if project_root else 0
        )
        churn_rate = (
            round((fc["lines_added"] + fc["lines_removed"]) / line_count, 2)
            if line_count > 0
            else 0.0
        )
        file_stats.append(
            {
                "file": filepath,
                "commits": fc["commits"],
                "lines_added": fc["lines_added"],
                "lines_removed": fc["lines_removed"],
                "churn_rate": churn_rate,
                "authors": len(fc["authors"]),
                "first_commit_in_range": fc["first_date"],
                "last_modified": fc["last_date"],
            }
        )

    file_stats.sort(key=lambda x: x["commits"], reverse=True)
    return commits, file_stats


def _relative_scope(scan_root: Path, project_root: Path) -> str:
    try:
        rel = scan_root.resolve().relative_to(project_root.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return "."


def get_function_boundaries(filepath: Path) -> list[dict]:
    """Get function boundaries using Tree-sitter for C or AST for Python."""
    if filepath.suffix in (".c", ".h"):
        return _get_c_function_boundaries(filepath)
    elif filepath.suffix == ".py":
        return _get_py_function_boundaries(filepath)
    return []


def _get_c_function_boundaries(filepath: Path) -> list[dict]:
    """Use Tree-sitter to get C function boundaries."""
    try:
        source_bytes = filepath.read_bytes()
        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        return [
            {
                "name": f["name"],
                "line_start": f["start_line"],
                "line_end": f["end_line"],
            }
            for f in functions
        ]
    except OSError:
        return []


def _get_py_function_boundaries(filepath: Path) -> list[dict]:
    """Use Python AST to get Python function boundaries."""
    import ast

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return []

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_lineno = getattr(node, "end_lineno", node.lineno)
            functions.append(
                {
                    "name": node.name,
                    "line_start": node.lineno,
                    "line_end": end_lineno,
                }
            )
    return functions


def compute_function_churn(commits, scan_root, project_root, max_files=0):
    """Map diff hunks to function boundaries using Tree-sitter/AST."""
    file_functions = {}
    if scan_root.is_file():
        all_files = [scan_root]
    else:
        all_files = sorted(
            p
            for p in scan_root.rglob("*")
            if p.is_file() and p.suffix in (".c", ".h", ".py")
        )

    exclude = {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".eggs",
        "build",
        "dist",
    }

    filtered = []
    for f in all_files:
        try:
            parts = set(f.relative_to(project_root).parts)
        except ValueError:
            continue
        if parts & exclude:
            continue
        filtered.append(f)

    if max_files > 0:
        filtered = filtered[:max_files]

    for f in filtered:
        try:
            rel_path = str(f.relative_to(project_root))
        except ValueError:
            rel_path = str(f)
        boundaries = get_function_boundaries(f)
        if boundaries:
            file_functions[rel_path] = boundaries

    if not file_functions:
        return []

    func_commits = defaultdict(set)
    for commit in commits:
        if _check_script_timeout():
            break
        for file_path in commit["files"]:
            if file_path not in file_functions:
                continue
            try:
                diff_result = _run_git(
                    ["show", "--format=", "-U0", commit["hash"], "--", file_path],
                    project_root,
                )
                if diff_result.returncode != 0:
                    continue
            except subprocess.TimeoutExpired:
                continue

            changed_lines = set()
            for line in diff_result.stdout.splitlines():
                hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
                if hunk:
                    start = int(hunk.group(1))
                    count = int(hunk.group(2)) if hunk.group(2) else 1
                    changed_lines.update(range(start, start + count))

            for func in file_functions[file_path]:
                func_range = set(range(func["line_start"], func["line_end"] + 1))
                if changed_lines & func_range:
                    func_commits[(file_path, func["name"])].add(commit["hash"])

    results = []
    for (file_path, func_name), commit_hashes in func_commits.items():
        boundaries = file_functions.get(file_path, [])
        func_info = next((f for f in boundaries if f["name"] == func_name), None)
        results.append(
            {
                "function": func_name,
                "file": file_path,
                "line_start": func_info["line_start"] if func_info else 0,
                "line_end": func_info["line_end"] if func_info else 0,
                "commits": len(commit_hashes),
            }
        )

    results.sort(key=lambda x: x["commits"], reverse=True)
    return results


def _truncate_diff(diff_text, max_lines):
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    return "\n".join(lines[:max_lines]) + "\n[diff truncated]"


def get_commit_details(commits, commit_type, project_root, scan_root, max_diff_lines):
    typed = [c for c in commits if c["type"] == commit_type]
    results = []
    rel_scope = _relative_scope(scan_root, project_root)

    for commit in typed:
        if _check_script_timeout():
            break
        diff_args = ["show", "--format=", "--patch", commit["hash"], "--"]
        if rel_scope != ".":
            diff_args.append(rel_scope)
        try:
            dr = _run_git(diff_args, project_root)
            diff_text = dr.stdout if dr.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            diff_text = "[diff unavailable: timeout]"

        diff_text = _truncate_diff(diff_text, max_diff_lines)

        results.append(
            {
                "commit": commit["hash"],
                "commit_short": commit["hash"][:7],
                "message": commit["message"],
                "date": commit["date"],
                "author": commit["author"],
                "files": commit["files"],
                "diff": diff_text,
            }
        )
    return results


def compute_co_change_clusters(commits, min_co_changes=3, max_pairs=30):
    file_commit_counts = defaultdict(int)
    co_changes = defaultdict(int)

    for commit in commits:
        files = sorted(set(commit["files"]))
        for f in files:
            file_commit_counts[f] += 1
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                co_changes[(files[i], files[j])] += 1

    results = []
    for (a, b), count in co_changes.items():
        if count >= min_co_changes:
            results.append(
                {
                    "file_a": a,
                    "file_b": b,
                    "co_change_count": count,
                    "total_commits_a": file_commit_counts[a],
                    "total_commits_b": file_commit_counts[b],
                }
            )
    results.sort(key=lambda x: x["co_change_count"], reverse=True)
    return results[:max_pairs]


def parse_args(argv):
    args = {
        "path": ".",
        "days": 90,
        "since": None,
        "until": None,
        "last": None,
        "max_commits": 2000,
        "max_files": 0,
        "no_function": False,
    }

    def _parse_int(flag: str, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise SystemExit(
                json.dumps({"error": f"{flag} requires an integer, got '{value}'"})
            )

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days" and i + 1 < len(argv):
            args["days"] = _parse_int("--days", argv[i + 1])
            i += 2
        elif arg == "--since" and i + 1 < len(argv):
            args["since"] = argv[i + 1]
            i += 2
        elif arg == "--until" and i + 1 < len(argv):
            args["until"] = argv[i + 1]
            i += 2
        elif arg == "--last" and i + 1 < len(argv):
            args["last"] = _parse_int("--last", argv[i + 1])
            i += 2
        elif arg == "--max-commits" and i + 1 < len(argv):
            args["max_commits"] = _parse_int("--max-commits", argv[i + 1])
            i += 2
        elif arg == "--max-files" and i + 1 < len(argv):
            args["max_files"] = _parse_int("--max-files", argv[i + 1])
            i += 2
        elif arg == "--no-function":
            args["no_function"] = True
            i += 1
        elif not arg.startswith("-"):
            args["path"] = arg
            i += 1
        else:
            i += 1
    return args


def analyze(argv=None):
    """Analyze git history for churn metrics and commit classification."""
    global _SCRIPT_START
    _SCRIPT_START = time.monotonic()

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)

    if not _is_git_repo(project_root):
        return {"error": "Not a git repository", "project_root": str(project_root)}

    now = datetime.now(timezone.utc)
    since = args["since"] or (now - timedelta(days=args["days"])).isoformat()
    until = args["until"] or now.isoformat()

    last_n = args["last"]
    max_commits = args["max_commits"]

    git_args = ["log", "--numstat", "--format=COMMIT:%H|%aI|%an|%s"]
    if last_n is not None:
        git_args.append(f"-{last_n}")
    else:
        git_args.extend([f"--since={since}", f"--until={until}"])
    git_args.append("--")
    rel_scope = _relative_scope(scan_root, project_root)
    if rel_scope != ".":
        git_args.append(rel_scope)

    proc = _run_git_streaming(git_args, project_root)
    try:
        commits, file_churn = parse_git_log(proc.stdout, max_commits, project_root)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    commit_cap_applied = len(commits) >= max_commits
    if last_n is not None and commits:
        since = commits[-1]["date"]
        until = commits[0]["date"]
        try:
            days = max(
                1, (datetime.fromisoformat(until) - datetime.fromisoformat(since)).days
            )
        except ValueError:
            days = args["days"]
    else:
        days = args["days"]

    commits_by_type = defaultdict(int)
    authors = set()
    for c in commits:
        commits_by_type[c["type"]] += 1
        authors.add(c["author"])

    function_churn = []
    function_churn_note = None
    if args["no_function"] or _check_script_timeout():
        function_churn_note = "Function-level churn skipped"
    else:
        function_churn = compute_function_churn(
            commits, scan_root, project_root, max_files=args["max_files"]
        )

    recent_fixes = get_commit_details(
        commits, "fix", project_root, scan_root, _MAX_DIFF_LINES_FIX
    )
    recent_features = get_commit_details(
        commits, "feature", project_root, scan_root, _MAX_DIFF_LINES_FIX
    )
    recent_refactors = get_commit_details(
        commits, "refactor", project_root, scan_root, _MAX_DIFF_LINES_REFACTOR
    )

    co_change_clusters = compute_co_change_clusters(commits)

    result = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {
            "start": since,
            "end": until,
            "days": days,
            "commit_cap_applied": commit_cap_applied,
        },
        "summary": {
            "total_commits": len(commits),
            "commits_by_type": dict(commits_by_type),
            "files_changed": len(file_churn),
            "functions_changed": len(function_churn),
            "authors": len(authors),
        },
        "file_churn": file_churn,
        "function_churn": function_churn,
        "recent_fixes": recent_fixes,
        "recent_features": recent_features,
        "recent_refactors": recent_refactors,
        "co_change_clusters": co_change_clusters,
    }

    if function_churn_note:
        result["function_churn_note"] = function_churn_note

    return result


def main() -> None:
    try:
        result = analyze()
        if "error" in result:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
