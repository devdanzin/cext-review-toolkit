---
description: "Find the worst functions to fix first -- refcount issues, error bugs, and complexity"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# C Extension Hotspots

Run the three highest-value agents to find the worst functions to fix first: **refcount-auditor**, **error-path-analyzer**, and **c-complexity-analyzer**. Answers the question: "Where should I focus my review efforts?"

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Run `python <plugin_root>/scripts/discover_extension.py [scope]` to detect the extension layout
2. If no C extension found, inform the user and stop
3. Run with at most 2 agents in parallel, feeding discovery context:
   - **refcount-auditor** -- find reference counting errors
   - **error-path-analyzer** -- find error handling bugs
   - **c-complexity-analyzer** -- find the hardest-to-maintain code
4. Synthesize into a prioritized hotspot report:

```markdown
# C Extension Hotspots

## Extension: [name]

## Critical Issues (FIX)
[Refcount leaks, NULL dereferences, error handling bugs]
- [agent]: Issue in `function` (file.c:line) -- [description]

## Complexity Hotspots
| Rank | Function | File | Score | Lines | Top Issue |
|------|----------|------|-------|-------|-----------|
| 1 | func | f.c | 8.5 | 450 | Deep nesting |

## Error-Prone Functions
[Functions with both high complexity AND refcount/error issues -- these are
the highest-priority targets because they're hard to reason about AND have bugs]

## Recommended Fix Order
1. [Highest-impact fix -- typically a FIX finding in a high-complexity function]
2. [Next]
3. [Next]

For detailed analysis of a specific aspect:
  /cext-review-toolkit:explore . refcounts deep
  /cext-review-toolkit:explore . errors deep
```

## Usage

```
/cext-review-toolkit:hotspots             # Entire project
/cext-review-toolkit:hotspots src/        # Specific directory
```
