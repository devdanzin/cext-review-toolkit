---
description: "Quick health dashboard -- all agents in summary mode"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# C Extension Health Dashboard

Run all agents in summary mode to produce a quick health dashboard. Each agent reports only its top-level findings -- no deep analysis.

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Run `python <plugin_root>/scripts/discover_extension.py [scope]` to detect the extension layout
2. If no C extension found, inform the user and stop
3. Run all analysis agents with context, requesting summary-tier output only. Run at most 2 concurrently to limit resource usage.
4. Deduplicate before scoring: when the same issue is flagged by multiple agents, count it once.
5. Synthesize into a health dashboard:

```markdown
# C Extension Health Dashboard

## Extension: [name] ([N] C files, [N] lines)

| Dimension | Status | Score | FIX | Top Finding |
|-----------|--------|-------|-----|-------------|
| Refcount Safety | G/Y/R | X/10 | N | [1-line summary] |
| Error Handling | G/Y/R | X/10 | N | [1-line summary] |
| NULL Safety | G/Y/R | X/10 | N | [1-line summary] |
| GIL Discipline | G/Y/R | X/10 | N | [1-line summary] |
| Module State | G/Y/R | X/10 | N | [1-line summary] |
| Type Slots | G/Y/R | X/10 | N | [1-line summary] |
| ABI Compliance | G/Y/R | X/10 | N | [1-line summary] |
| Version Compat | G/Y/R | X/10 | N | [1-line summary] |
| Complexity | G/Y/R | X/10 | N | [1-line summary] |

## Overall Health: X/10

## Extension Profile
- Init style: [single-phase / multi-phase]
- Python targets: [version range]
- Limited API: [yes/no]
- Types defined: [N]

## Top 3 Priorities
1. [Most impactful improvement]
2. [Next]
3. [Next]

For detailed analysis, run:
  /cext-review-toolkit:explore . [aspect] deep
```

## Scoring Rubric

Each dimension is scored 1-10:

- **10**: Exceptional -- no findings above ACCEPTABLE
- **8-9**: Healthy -- only CONSIDER-level findings
- **6-7**: Good with gaps -- a few FIX items
- **4-5**: Concerning -- multiple FIX items
- **2-3**: Problematic -- many FIX items or systemic issues
- **1**: Severe -- fundamental correctness issues

Score deductions:
- Each FIX finding: -0.5 to -1.0
- Systemic CONSIDER pattern: -0.5
- Individual CONSIDER finding: -0.1 to -0.2

G = 8-10 | Y = 5-7 | R = 1-4

## Usage

```
/cext-review-toolkit:health              # Full project health
/cext-review-toolkit:health src/         # Specific directory
```
