"""Tests for analyze_history.py — git history analysis."""

import os
import subprocess
import unittest
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


class TestAnalyzeHistory(unittest.TestCase):
    """Test git history analysis."""

    def test_not_git_repo(self):
        """Non-git directory returns error."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = history.analyze([str(root)])
            self.assertIn("error", result)

    def test_basic_history_analysis(self):
        """Basic git history analysis works."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
            v2 = MINIMAL_EXTENSION.replace('"hello"', '"world"')
            _make_commit(root, "fix: correct greeting string",
                         {"myext.c": v2})
            _make_commit(root, "add: new feature",
                         {"myext.c": v2 + "\n/* new */\n"})

            result = history.analyze([str(root), "--last", "10"])
            self.assertNotIn("error", result)
            self.assertGreaterEqual(result["summary"]["total_commits"], 2)
            self.assertIn("file_churn", result)
            self.assertIn("recent_fixes", result)

    def test_commit_classification(self):
        """Commits are classified correctly."""
        self.assertEqual(history.classify_commit("fix: null pointer crash"), "fix")
        self.assertEqual(history.classify_commit("add: new module"), "feature")
        self.assertEqual(history.classify_commit("refactor: simplify init"), "refactor")
        self.assertEqual(history.classify_commit("update docs"), "docs")
        self.assertEqual(history.classify_commit("bump version"), "chore")
        self.assertEqual(history.classify_commit("some random change"), "unknown")

    def test_c_function_boundaries(self):
        """Tree-sitter-based C function boundary detection works."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            boundaries = history.get_function_boundaries(root / "myext.c")
            names = [b["name"] for b in boundaries]
            self.assertIn("myext_hello", names)
            self.assertIn("PyInit_myext", names)

    def test_empty_repo(self):
        """Empty repo with no extra commits handled gracefully."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
            result = history.analyze([str(root), "--last", "10"])
            self.assertNotIn("error", result)
            self.assertGreaterEqual(result["summary"]["total_commits"], 1)

    def test_output_structure(self):
        """Output has expected top-level fields."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}, init_git=True) as root:
            result = history.analyze([str(root), "--last", "5"])
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("time_range", result)
            self.assertIn("summary", result)
            self.assertIn("file_churn", result)
            self.assertTrue(
                "function_churn" in result or "function_churn_note" in result)

    def test_parse_git_log_well_formed(self):
        """parse_git_log handles well-formed multi-commit log."""
        lines = [
            "COMMIT:abc123|2025-03-01T00:00:00+00:00|Alice|fix: null check\n",
            "5\t2\tsrc/myext.c\n",
            "COMMIT:def456|2025-03-02T00:00:00+00:00|Bob|add: feature\n",
            "10\t0\tsrc/myext.c\n",
            "3\t1\tsrc/util.c\n",
        ]
        commits, file_stats = history.parse_git_log(lines, max_commits=100)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["hash"], "abc123")
        self.assertEqual(commits[0]["type"], "fix")
        self.assertEqual(commits[1]["hash"], "def456")
        self.assertEqual(len(commits[1]["files"]), 2)
        files = {fs["file"] for fs in file_stats}
        self.assertIn("src/myext.c", files)

    def test_parse_git_log_empty(self):
        """parse_git_log handles empty input."""
        commits, file_stats = history.parse_git_log([], max_commits=100)
        self.assertEqual(commits, [])
        self.assertEqual(file_stats, [])

    def test_parse_git_log_binary_file(self):
        """parse_git_log handles binary file lines (added/removed are '-')."""
        lines = [
            "COMMIT:abc123|2025-03-01T00:00:00+00:00|Alice|add: binary\n",
            "-\t-\tdata/image.png\n",
            "5\t0\tsrc/main.c\n",
        ]
        commits, file_stats = history.parse_git_log(lines, max_commits=100)
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(commits[0]["files"]), 2)
        binary_stat = [s for s in commits[0]["stats"]
                       if s["file"] == "data/image.png"][0]
        self.assertEqual(binary_stat["added"], 0)
        self.assertEqual(binary_stat["removed"], 0)


if __name__ == "__main__":
    unittest.main()
