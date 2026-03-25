---
description: "Comprehensive C extension analysis using specialized agents. Use when the user asks to analyze, audit, or review a C extension, find bugs in C extension code, run all checks on an extension, or do a full extension review. Covers refcount safety, error handling, NULL safety, GIL discipline, module state, type slots, ABI compliance, version compatibility, PyErr_Clear auditing, resource lifecycle, and C/Python parity."
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Comprehensive C Extension Analysis

Run a comprehensive analysis of a CPython C extension using multiple specialized agents, each focusing on a different aspect of extension correctness. Extension discovery runs first to understand the project layout.

**Arguments:** "$ARGUMENTS"

**Plugin root:** `<plugin_root>` refers to the directory containing this command file's parent -- i.e., the `plugins/cext-review-toolkit/` directory. Resolve it relative to this file's location.

## Argument Parsing

Parse arguments into three categories:

**Scope** (path or glob):
- `.` or omitted → entire project (default)
- `src/` → specific directory tree
- `src/myext.c` → specific file

**Aspects** (which agents to run):
- `refcounts` → refcount-auditor
- `errors` → error-path-analyzer
- `null-safety` → null-safety-scanner
- `gil` → gil-discipline-checker
- `module-state` → module-state-checker
- `type-slots` → type-slot-checker
- `abi` → stable-abi-checker
- `compat` → version-compat-scanner
- `pyerr-clear` → pyerr-clear-auditor
- `resources` → resource-lifecycle-checker
- `parity` → parity-checker
- `complexity` → c-complexity-analyzer
- `history` → git-history-analyzer
- `external-tools` → run external tools only (clang-tidy, cppcheck)
- `all` → all agents (default)

**Options**:
- `deep` → full detail, no output truncation
- `summary` → summary tier only (faster)
- `parallel` → run agents concurrently where possible
- `--max-parallel N` → cap concurrent agents per group (default: 2)

## Execution Workflow

### Phase 0: Extension Discovery

Before launching any agents:
1. Run `python <plugin_root>/scripts/discover_extension.py [scope]` to detect the extension layout
2. Parse the JSON output to identify: module names, source files, init style, Python targets, limited API status
3. Count .c and .h files in scope
4. Check the `code_generation` field: `"hand_written"`, `"cython"`, `"mypyc"`, `"pybind11"`, or `"mixed"`
5. Print a brief project summary:

```
Extension: myext (3 C files, 2,500 lines)
Init style: single-phase
Python targets: >=3.9
Limited API: no
Code generation: hand_written
```

If no C extension source files are found, inform the user and suggest checking the scope.

### Code Generation Strategy

When `code_generation` is `"cython"` or `"mypyc"`, adapt the agent dispatch:

**Skip these agents** (95-100% false positive rate on generated code):
- refcount-auditor (Cython/mypyc manage refcounts)
- error-path-analyzer (generated error handling is mechanical)
- null-safety-scanner (generated NULL checks use different patterns)
- module-state-checker (answers are always "generator limitation")
- stable-abi-checker (generated code doesn't target stable ABI)
- version-compat-scanner (generated code handles version compat at the generator level)

**Keep these agents** (still valuable on generated code):
- type-slot-checker (near-zero FP, finds real bugs in generated dealloc/traverse)
- gil-discipline-checker (GIL issues can exist in Cython `nogil` blocks)
- c-complexity-analyzer (generated code complexity is informational)
- git-history-analyzer (always valuable — finds bugs via historical patterns)

**For `"pybind11"`**: Run all agents normally (pybind11 code is closer to hand-written).

**For `"mixed"`**: Run all agents but note in the prompt which files are generated so agents can adjust their triage expectations.

Tell agents what code generation tool is in use so they can calibrate their confidence levels and focus on patterns specific to that generator.

### Phase 0.5: External Tool Baseline (Optional)

After extension discovery, check for external tool availability:
1. Check if `compile_commands.json` exists in the project root or common build directories (build/, _build/, builddir/)
2. If found: run `python <plugin_root>/scripts/run_external_tools.py [scope] --compile-commands <path>`
3. If not found: run `python <plugin_root>/scripts/run_external_tools.py [scope]` (cppcheck can still run without it)
4. Store the output -- individual agents in Phase 2 will cross-reference these findings
5. Print a brief tool summary:

```
External tools: clang-tidy (with compile_commands.json), cppcheck
External findings: 3 clang-tidy, 5 cppcheck
```

If no external tools are available, note it and continue without:
```
External tools: none available (install clang-tidy and/or cppcheck for enhanced analysis)
```

### Phase 1: Temporal Context (if git repo)

If the project is a git repository:
- Note that git history is available and will be used by git-history-analyzer in Phase 2F
- Do NOT run git history analysis as a separate step (the git-history-analyzer agent handles its own script)

### Phase 2: Targeted Analysis

Based on the requested aspects (default: all), launch the appropriate agents. Each agent receives the specified scope and the extension discovery output as context.

**Agent dispatch order** (sequential by default):

**Group A -- Safety-critical analysis** (highest value):
1. refcount-auditor
2. error-path-analyzer

**Group B -- Memory safety**:
3. null-safety-scanner
4. gil-discipline-checker
5. resource-lifecycle-checker

**Group C -- Extension correctness**:
6. module-state-checker
7. type-slot-checker
8. pyerr-clear-auditor

**Group D -- Compatibility and parity**:
9. stable-abi-checker
10. version-compat-scanner
11. parity-checker (only for extensions with dual C/Python implementations)

**Group E -- Code quality**:
12. c-complexity-analyzer

**Group F -- History** (runs last, benefits from all prior findings):
13. git-history-analyzer

If `parallel` is specified, run agents within each group concurrently (at most `--max-parallel` agents per group, default 2). Groups still execute sequentially.

### Phase 3: Synthesis

After all agents complete, perform deduplication, conflict resolution, and produce a unified summary.

#### Deduplication and Conflict Resolution

1. **Merge overlapping findings**: When two agents flag the same file:line, merge them:
   ```
   - [refcount-auditor, error-path-analyzer]: Missing DECREF for `item`
     on error path in process_data (src/myext.c:142)
   ```

2. **Surface contradictions**: When agents disagree:
   ```
   ## Tensions
   - **Module state vs. simplicity** at src/myext.c:
     module-state-checker flags single-phase init.
     c-complexity-analyzer shows low complexity throughout.
     -> Single-phase init is simpler and the extension is small. Migration
       is only worthwhile if subinterpreter support is needed.
   ```

3. **Attribute to most specific agent**: Type slot issues -> type-slot-checker, not error-path-analyzer.

#### Summary Template

```markdown
# C Extension Analysis Report

## Extension: [name]
## Scope: [what was analyzed]
## Agents Run: [list]

## Executive Summary
[3-5 sentences: overall extension health, most critical findings, key recommendations]

## Extension Profile
- Module: [name] ([N] C files, [N] lines)
- Init style: [single-phase / multi-phase]
- Python targets: [version range]
- Limited API: [yes/no]
- Types defined: [N]

## Key Metrics
| Dimension | Status | FIX | CONSIDER | Top Finding |
|-----------|--------|-----|----------|-------------|
| Refcount Safety | G/Y/R | N | N | [1-line summary] |
| Error Handling | G/Y/R | N | N | [1-line summary] |
| NULL Safety | G/Y/R | N | N | [1-line summary] |
| GIL Discipline | G/Y/R | N | N | [1-line summary] |
| Resource Lifecycle | G/Y/R | N | N | [1-line summary] |
| Module State | G/Y/R | N | N | [1-line summary] |
| Type Slots | G/Y/R | N | N | [1-line summary] |
| PyErr_Clear Safety | G/Y/R | N | N | [1-line summary] |
| ABI Compliance | G/Y/R | N | N | [1-line summary] |
| Version Compat | G/Y/R | N | N | [1-line summary] |
| C/Python Parity | G/Y/R | N | N | [1-line summary] |
| Complexity | G/Y/R | - | N | [1-line summary] |

G = No FIX findings | Y = 1-3 FIX findings | R = 4+ FIX findings

## Findings by Priority

### Must Fix (FIX)
[Crash risks, memory corruption, reference counting bugs]

### Should Consider (CONSIDER)
[Improvement opportunities, modernization, compatibility]

### Tensions
[Where agents disagree or trade-offs exist]

### Policy Decisions (POLICY)
[Team-level decisions: init style, ABI, version support]

## Strengths
[What the extension does well -- correct patterns, good error handling, etc.]

## Recommended Action Plan

### Immediate (FIX items)
1. [Highest-impact safety fix]
2. [Next]

### Short-term (CONSIDER items)
1. [Quality improvement]
2. [Modernization step]

### Longer-term (POLICY)
1. [Strategic decisions]
```

## How Extension Discovery Context Flows

When passing discovery output to agents:

```
[Extension discovery output]

The above is the extension layout analysis. Use it to:
- Understand which files are part of the extension vs. vendored/third-party code
- Know the init style (single-phase vs multi-phase) for context
- Know the target Python versions for compatibility assessment
- Know whether limited API is claimed for ABI checking
```

## Usage Examples

**Full exploration:**
```
/cext-review-toolkit:explore
```

**Specific scope:**
```
/cext-review-toolkit:explore src/
```

**Safety-only analysis:**
```
/cext-review-toolkit:explore . refcounts errors null-safety
```

**Extension correctness check:**
```
/cext-review-toolkit:explore . module-state type-slots
```

**Compatibility audit:**
```
/cext-review-toolkit:explore . abi compat
```

**Quick summary:**
```
/cext-review-toolkit:explore . all summary
```

**Deep dive:**
```
/cext-review-toolkit:explore . all deep
```

## Tips

- **Start with `summary` for unfamiliar extensions**: Get the lay of the land before deep diving
- **Safety first**: `refcounts errors` finds the most impactful bugs
- **Before a Python version upgrade**: `compat abi` identifies what needs to change
- **Before adding subinterpreter support**: `module-state type-slots` identifies migration work
- **For large extensions**: Use `--max-parallel 3` to speed up analysis
