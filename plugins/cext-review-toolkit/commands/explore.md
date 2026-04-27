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
- `--runs N` → run agents N times (default: 1). Run 2+ are independent naive passes. Findings are deduplicated across runs. If run 2 finds 0 new findings, report "high confidence — converged on first pass."
- `--informed-reruns` → when used with `--runs 3`, run 3 agents receive a summary of runs 1-2 findings with the instruction: "These bugs were already found. Look at ADJACENT code, unexplored error paths, and patterns similar to these confirmed bugs that the prior runs might have missed." This targets the informed-rerun technique that found 33% more bugs on simplejson.

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

When `code_generation` is `"cython"` or `"mypyc"`, adapt the agent dispatch. The strategy is calibrated against a comprehensive uvloop review (see `reports/uvloop_v3/final_report.md`) where every "skipped" agent was evaluated with one naive pass to verify the skip default produces no FIX-class loss.

**Always run on Cython** (FIX-class output, real bugs caught):
- **type-slot-checker** — near-zero FP; finds real bugs in dealloc, partial-init paths, INCREF ordering, `@cython.no_gc_clear` pair breaks. (uvloop: 1 FIX HIGH `_UDPSendContext.new`; 4 CONSIDER on SSLProtocol pair, raise-in-dealloc, UVRequest pin)
- **gil-discipline-checker** — finds non-atomic FT-counter holdouts, fork-handler globals, freelist races. (uvloop: 4 CONSIDER on FT-readiness gaps)
- **resource-lifecycle-checker** — finds libuv handle leaks, partial-init cleanup gaps, FD-window leaks. (uvloop: 2 FIX HIGH on `Loop.__dealloc__`, `_StreamWriteContext.new`; 4 CONSIDER on twin sites)
- **git-history-analyzer** — finds fix-completeness gaps, regression dates, twin-class fix asymmetry. (uvloop: 1 FIX HIGH on 25-site atomic-counter gap)
- **parity-checker** — adapted Cython mode reads `.pyi` stubs to find advertised-but-unimplemented APIs. **No other agent reads `.pyi`.** (uvloop: 1 FIX HIGH `sock_recvfrom`/`sock_sendto`/`sock_recvfrom_into` raise NotImplementedError but `.pyi` advertises them; 1 CONSIDER `sendfile`/`sock_sendfile` undocumented gap)

**Skip by default on Cython** (95-100% FP rate from generator-emitted patterns; validated zero FIX-class loss):
- **refcount-auditor** — Cython runtime `__Pyx_INCREF`/`__Pyx_DECREF` are noise. Maintainer-written manual `Py_INCREF`/`Py_DECREF` patterns ARE catchable but type-slot-checker covers them in factory-method audits. (uvloop eval: 0 FIX, 1 CONSIDER overlapping with kept agents)
- **error-path-analyzer** — generated `goto __PyX_L*_error;` cleanup is mechanical. (uvloop eval: 0 FIX, 1 trivial CONSIDER)
- **null-safety-scanner** — Cython's `if (unlikely(!__pyx_v_X))` patterns are mechanical. (uvloop eval: 0 FIX, 1 LOW CONSIDER)
- **pyerr-clear-auditor** — Cython runtime `PyErr_Clear` calls are noise; maintainer-written ones in `.pyx` are rare and other agents catch the analog. (uvloop eval: 0 findings)

**Run on Cython for deep-effort reviews** (low FIX yield but unique CONSIDER/POLICY surface):
- **module-state-checker** — finds `static PyObject*` cached imports outside module state, module-level `cdef` C globals, subinterpreter blockers. Skip default works for FIX-only reviews; deep reviews benefit. (uvloop eval: 1 CONSIDER `stdlib.pxi` ~100 cached imports; subinterpreter assessment)
- **c-complexity-analyzer** — adapt to walk `.pyx` indent structure rather than C-level (Cython codegen artifacts inflate C-level metrics). Finds maintainer-visible refactor targets. (uvloop eval: 3 CONSIDER on `Loop.create_server`, `__convert_pyaddr_to_sockaddr`, `Loop.create_connection`)
- **version-compat-scanner** — Cython 4 readiness, deprecated directives, dead `PY_VERSION_HEX` guards in hand-written `includes/*.h`, private-API `_Py_*` reimplementations. (uvloop eval: 1 CONSIDER dead guard, 1 POLICY `_Py_RestoreSignals` reimplementation)
- **stable-abi-checker** — distinguish maintainer abi3 claim from Cython runtime opt-in (`cython_limited_api=True`); audit hand-written C for non-stable macros; produce feasibility assessment. (uvloop eval: 1 POLICY abi3 not feasible due to libuv coupling)

**For `"pybind11"`**: Run all agents normally (pybind11 code is closer to hand-written).

**For `"mixed"`**: Run all agents but note in the prompt which files are generated so agents can adjust their triage expectations.

When dispatching kept agents on a Cython project, **tell each agent the code-generation tool, point to `reports/<extension>_v?/preflight/generated_code_map.md` if it exists, and ask them to apply Cython-mode triage** (the agent prompts have a "Cython-mode" section documenting what survives the generator filter). For deep-effort reviews, also pass the `cython_kept_agents.md` playbook reference if available.

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

**Use global non-restarting numbering**: number ALL findings sequentially across all sections. FIX findings first (1-N), then CONSIDER (N+1-M), then POLICY (M+1-P). Use these same numbers in the action plan. This makes it easy to reference "Finding 37" in issue trackers and emails.

### Must Fix (FIX) — N

| # | Finding | File:Line | Agents |
|---|---------|-----------|--------|
| 1 | [Description] | [file:line] | [which agents found it] |

### Should Consider (CONSIDER) — M

| # | Finding | File:Line |
|---|---------|-----------|
| N+1 | [Description] | [file:line] |

### Tensions
[Where agents disagree or trade-offs exist]

### Policy Decisions (POLICY) — P

| # | Finding |
|---|---------|
| M+1 | [Description] |

## Strengths
[What the extension does well -- correct patterns, good error handling, etc.]

## Code Removal Opportunities

Check `<plugin_root>/data/deprecated_apis.json` `code_removal_opportunities` section. For each entry, search the codebase for the `replaces_pattern` and report how many lines could be removed. Example:

- **PyModule_AddType** (3.10+): replaces `PyType_FromSpec` + `PyType_Ready` + `Py_INCREF` + `PyModule_AddObject` chain. Est. 4-8 lines saved per type.
- **PyImport_ImportModuleAttrString** (3.14+): replaces `PyImport_ImportModule` + `PyObject_GetAttrString` + `Py_DECREF(module)`. Est. 5-8 lines per import+getattr.

Only include entries where the extension's minimum Python version supports the replacement API.

## Recommended Action Plan

Reference findings by their global number:

### Immediate (FIX items)
1. [Fix Finding N — description]
2. [Fix Finding M — description]

### Short-term (CONSIDER items)
1. [Finding N+1 — description]
2. [Finding N+2 — description]

### Longer-term (POLICY)
1. [Finding M+1 — description]
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
