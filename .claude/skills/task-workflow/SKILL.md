---
name: task-workflow
description: Execute the standard issue, branch, code, test, commit, PR, merge workflow for a task
argument-hint: <task-description>
disable-model-invocation: false
user-invocable: true
---

# Task Workflow

Execute the standard development workflow for this task: `$ARGUMENTS`

Follow these steps in order. Do NOT skip any step.

## Step 1: Create GitHub Issue

```bash
gh issue create --title "<title>" --body "<description>" --label "<label>"
```

Common labels: `enhancement`, `bug`, `tests`, `documentation`, `refactor`

Use the task description to write a clear issue title and body. The body should explain the context, what needs to change, and why.

## Step 2: Create Feature Branch

Branch naming convention:
- `feat/<slug>` for new features
- `fix/<slug>` for bug fixes
- `refactor/<slug>` for refactoring
- `test/<slug>` for test-only changes
- `docs/<slug>` for documentation

```bash
git checkout -b <prefix>/<short-slug>
```

## Step 3: Implement Changes

Read existing code before modifying it. Follow patterns already established in the codebase. Key conventions:
- Line length: 99 characters
- Double quotes for strings
- Complete type hints on all functions
- Docstrings on classes and public methods
- Tests use `pytest` — no unittest
- Add an entry to `CHANGELOG.md` under `## [Unreleased]` in the appropriate section (Added/Enhanced/Fixed/Documentation). Format: `- Description.`

## Step 4: Verify

Run all verification commands. ALL must pass before committing.

Important: The `.venv` uses a JIT-enabled CPython build with AddressSanitizer. All commands require the `ASAN_OPTIONS=detect_leaks=0 PYTHON_JIT=0` prefix.

```bash
ASAN_OPTIONS=detect_leaks=0 PYTHON_JIT=0 .venv/bin/ruff format <changed-files>
ASAN_OPTIONS=detect_leaks=0 PYTHON_JIT=0 .venv/bin/ruff check <changed-files>
ASAN_OPTIONS=detect_leaks=0 PYTHON_JIT=0 .venv/bin/mypy
ASAN_OPTIONS=detect_leaks=0 PYTHON_JIT=0 .venv/bin/python -m pytest tests/ -v
```

If any tests fail, fix them before proceeding. If `ruff format` changes files, that's fine — they'll be committed in the next step.

## Step 5: Commit

Stage only the files you changed. Use conventional commit format. Always include the issue reference.

```bash
git add <specific-files>
git commit -m "$(cat <<'EOF'
<type>: <description>

<optional body>

Closes #<issue-number>

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

## Step 6: Push and Create PR

```bash
git push -u origin <branch-name>
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points>

## Test plan
- [x] <verification items>

Closes #<issue-number>

Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Step 7: Merge and Cleanup

```bash
gh pr merge <pr-number> --merge
git checkout main
git pull
git push origin --delete <branch-name>
```

## Step 8: Report

Tell the user:
- Issue URL
- PR URL (merged)
- Summary of changes made
- Test results (pass count)
- Changelog entry
