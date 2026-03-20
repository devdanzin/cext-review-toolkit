"""Tests for analyze_history.py — git history analysis."""

import os
import subprocess
import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

history = import_script("analyze_history")

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _make_commit(root, message, files=None):
    """Create a commit in the test repo."""
    if files:
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=str(root), capture_output=True, env=GIT_ENV,
    )


def test_not_git_repo():
    """Non-git directory returns error."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = history.analyze([str(root)])
        assert "error" in result


def test_basic_history_analysis():
    """Basic git history analysis works."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
        # Add a few more commits.
        v2 = MINIMAL_EXTENSION.replace('"hello"', '"world"')
        _make_commit(root, "fix: correct greeting string",
                     {"myext.c": v2})
        _make_commit(root, "add: new feature",
                     {"myext.c": v2 + "\n/* new */\n"})

        result = history.analyze([str(root), "--last", "10"])
        assert "error" not in result
        assert result["summary"]["total_commits"] >= 2
        assert "file_churn" in result
        assert "recent_fixes" in result


def test_commit_classification():
    """Commits are classified correctly."""
    assert history.classify_commit("fix: null pointer crash") == "fix"
    assert history.classify_commit("add: new module") == "feature"
    assert history.classify_commit("refactor: simplify init") == "refactor"
    assert history.classify_commit("update docs") == "docs"
    assert history.classify_commit("bump version") == "chore"
    assert history.classify_commit("some random change") == "unknown"


def test_c_function_boundaries():
    """Tree-sitter-based C function boundary detection works."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        boundaries = history.get_function_boundaries(root / "myext.c")
        names = [b["name"] for b in boundaries]
        assert "myext_hello" in names
        assert "PyInit_myext" in names


def test_empty_repo():
    """Empty repo with no extra commits handled gracefully."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
        result = history.analyze([str(root), "--last", "10"])
        assert "error" not in result
        assert result["summary"]["total_commits"] >= 1


def test_output_structure():
    """Output has expected top-level fields."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
        result = history.analyze([str(root), "--last", "5"])
        assert "project_root" in result
        assert "scan_root" in result
        assert "time_range" in result
        assert "summary" in result
        assert "file_churn" in result
        assert "function_churn" in result or "function_churn_note" in result


def test_parse_git_log_well_formed():
    """parse_git_log handles well-formed multi-commit log."""
    lines = [
        "COMMIT:abc123|2025-03-01T00:00:00+00:00|Alice|fix: null check\n",
        "5\t2\tsrc/myext.c\n",
        "COMMIT:def456|2025-03-02T00:00:00+00:00|Bob|add: feature\n",
        "10\t0\tsrc/myext.c\n",
        "3\t1\tsrc/util.c\n",
    ]
    commits, file_stats = history.parse_git_log(lines, max_commits=100)
    assert len(commits) == 2
    assert commits[0]["hash"] == "abc123"
    assert commits[0]["type"] == "fix"
    assert commits[1]["hash"] == "def456"
    assert len(commits[1]["files"]) == 2
    # File stats should have myext.c and util.c.
    files = {fs["file"] for fs in file_stats}
    assert "src/myext.c" in files


def test_parse_git_log_empty():
    """parse_git_log handles empty input."""
    commits, file_stats = history.parse_git_log([], max_commits=100)
    assert commits == []
    assert file_stats == []


def test_parse_git_log_binary_file():
    """parse_git_log handles binary file lines (added/removed are '-')."""
    lines = [
        "COMMIT:abc123|2025-03-01T00:00:00+00:00|Alice|add: binary\n",
        "-\t-\tdata/image.png\n",
        "5\t0\tsrc/main.c\n",
    ]
    commits, file_stats = history.parse_git_log(lines, max_commits=100)
    assert len(commits) == 1
    assert len(commits[0]["files"]) == 2
    # Binary file should have 0 added/removed.
    binary_stat = [s for s in commits[0]["stats"] if s["file"] == "data/image.png"][0]
    assert binary_stat["added"] == 0
    assert binary_stat["removed"] == 0
