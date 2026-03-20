# cext-review-toolkit — Design Document

## 1. Project Identity

**Name:** cext-review-toolkit
**Purpose:** A Claude Code plugin for reviewing CPython C extensions — finding API misuse, memory safety bugs, compatibility issues, and correctness problems that are specific to code that *consumes* the Python/C API rather than *implementing* it.

**Tagline:** *Find the bugs in your C extension before your users do.*

### 1.1 Relationship to Sibling Projects

| Project | Target | Parsing | Key Concern |
|---------|--------|---------|-------------|
| code-review-toolkit | Python source | `ast` module | Logic errors, dead code, test gaps |
| cpython-review-toolkit | CPython runtime C code | Regex (PEP 7) | Refcount leaks, GIL, NULL safety |
| **cext-review-toolkit** | **C extensions** | **Tree-sitter** | **API misuse, borrowed ref lifetime, module state, type slots, ABI compliance** |

The key architectural distinction: cpython-review-toolkit uses regex-based scanning because PEP 7 code is extremely regular. C extension code is not — it has wildly varying styles, heavy macro usage, and unconventional formatting. Tree-sitter absorbs that variance because it parses the actual C grammar, giving agents structured syntax trees instead of fragile regex matches.

### 1.2 Audience

- C extension authors (library maintainers, scientific computing developers)
- Contributors reviewing C extensions in third-party projects
- Teams migrating extensions to newer Python versions or the stable ABI
- Developers preparing extensions for free-threaded Python (PEP 703)

### 1.3 Non-Goals

- Replacing clang-tidy or cppcheck for general C analysis
- Reviewing pure Python code (use code-review-toolkit)
- Reviewing CPython's own source (use cpython-review-toolkit)
- Requiring a buildable environment (must work on raw source files)


## 2. Architecture

### 2.1 Parsing Layer: Tree-sitter

All scripts use Tree-sitter as the primary parsing backend. This is the foundational difference from cpython-review-toolkit.

**Dependencies:** `tree-sitter` and `tree-sitter-c` (pip-installable, pure wheels on most platforms, no system dependencies).

**What Tree-sitter gives us that regex cannot:**

| Capability | Regex | Tree-sitter |
|------------|-------|-------------|
| Extract function bodies | Fragile brace-counting | Reliable AST node extraction |
| Parse struct initializers (PyType_Spec, PyMethodDef) | Pattern-match field names | Walk struct members, resolve values |
| Track variable assignments within a function | Line-by-line scanning | Statement-level AST traversal |
| Identify scope (file-level static vs local) | Heuristic | Definitive from parse tree |
| Handle macro-heavy code | High false positive rate | Parses around macros, tolerates unknowns |
| Handle varying code styles | Tuned for PEP 7 only | Language-grammar-based, style-agnostic |

**Shared parsing utilities** (new module: `tree_sitter_utils.py`):

```
parse_file(path) → Tree
extract_functions(tree) → list[FunctionNode]
extract_struct_initializers(tree, type_name) → list[InitializerNode]
extract_static_declarations(tree) → list[DeclarationNode]
find_calls_in_function(func, api_name) → list[CallNode]
get_statements_between(func, start_node, end_node) → list[StatementNode]
variable_assignments(func, var_name) → list[AssignmentNode]
```

**Fallback:** If Tree-sitter is not installed, scripts print a clear error: `"cext-review-toolkit requires tree-sitter and tree-sitter-c: pip install tree-sitter tree-sitter-c"` and exit. No silent degradation to regex — the whole point of this project is better parsing.

### 2.2 Shared Data

API classification tables are shared with cpython-review-toolkit and maintained as JSON data files:

```
data/
├── api_tables.json          # NEW_REF_APIS, BORROWED_REF_APIS, STEAL_REF_APIS
├── deprecated_apis.json     # Deprecated APIs with version and replacement
├── stable_abi.json          # Functions in the stable ABI, by version
└── limited_api_headers.json # Headers permitted under Py_LIMITED_API
```

These files are vendored (copied) into both projects. When a new CPython release adds or deprecates APIs, the data file is updated and copied to both. The synchronization surface is just these data files, not analysis logic.

### 2.3 External Tool Integration (Optional)

External tools are opportunistic — used when available, never required.

| Tool | Detection | What It Adds | Fallback |
|------|-----------|-------------|----------|
| clang-tidy | Check for `compile_commands.json` or try `bear make` | Data-flow analysis, deeper null/UB detection | Tree-sitter-based analysis only |
| cppcheck | Check if `cppcheck` is on PATH | Buffer overflows, uninitialized vars | Tree-sitter-based analysis only |

Agent prompts note whether external tool results are available:
- "clang-tidy results available — higher confidence on data-flow findings"
- "No external tools available — analysis based on Tree-sitter structural scanning"

### 2.4 Project Discovery

Unlike cpython-review-toolkit (which looks for `Include/Python.h` + `Objects/object.c`), this toolkit must detect C extensions in diverse project layouts.

**Detection strategy (first match wins):**

1. `setup.py` with `ext_modules` — parse to find C source files
2. `pyproject.toml` with `[tool.meson-python]` or `[tool.setuptools.ext-modules]`
3. `meson.build` with `py.extension_module()`
4. Any `.c` file containing `#include <Python.h>` or `#include "Python.h"`
5. Manual scope: user points to a directory or file

The discovery script produces:
- List of C extension source files
- Module name(s) (from init function names or build config)
- Python version targets (from `python_requires`, `Py_LIMITED_API` defines, version guards)
- Whether the extension claims limited API / stable ABI compliance


## 3. Agents

### 3.1 Agent Overview

8 agents total — 6 script-backed, 2 qualitative.

#### Safety-Critical (script-backed, Tree-sitter-powered)

| Agent | What It Finds | Script | Adapted From |
|-------|--------------|--------|--------------|
| **refcount-auditor** | Leaked refs, borrowed-ref-across-callback, stolen-ref misuse, missing Py_CLEAR | `scan_refcounts.py` | cpython-review-toolkit (rewritten for caller perspective) |
| **error-path-analyzer** | Missing NULL checks, exception clobbering, return-without-exception | `scan_error_paths.py` | cpython-review-toolkit (rewritten for caller perspective) |
| **null-safety-scanner** | Unchecked allocations, deref-before-check, unchecked PyArg_Parse | `scan_null_checks.py` | cpython-review-toolkit (minor tuning) |
| **gil-discipline-checker** | GIL released during Python API calls, blocking I/O with GIL held, PyGILState issues, free-threading readiness | `scan_gil_usage.py` | cpython-review-toolkit (rewritten for extension patterns) |

#### Extension-Specific (script-backed, Tree-sitter-powered, new)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **module-state-checker** | Legacy single-phase init, global PyObject* state, missing m_traverse/m_clear/m_free, static mutable state | `scan_module_state.py` |
| **type-slot-checker** | Missing/incorrect tp_dealloc, tp_traverse not visiting all members, broken tp_richcompare, incorrect tp_new for heap types, missing Py_TPFLAGS | `scan_type_slots.py` |

#### Compatibility (qualitative — no script)

| Agent | What It Finds |
|-------|--------------|
| **stable-abi-checker** | Limited API violations: internal struct access, private API calls, wrong headers |
| **version-compat-scanner** | API calls without version guards, removed APIs, missing fallbacks |

#### History (script-backed)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **git-history-analyzer** | Similar bugs elsewhere in the extension, churn-based risk prioritization | `analyze_history.py` |

### 3.2 Agent Details

#### 3.2.1 refcount-auditor (rewritten for extensions)

**Key difference from cpython-review-toolkit version:** The CPython auditor looks at code that *implements* APIs with reference semantics. The extension auditor looks at code that *calls* those APIs and must handle the semantics correctly.

**Extension-specific patterns to detect:**

1. **Borrowed reference held across callback into Python.** This is the #1 extension-specific refcount bug. Tree-sitter enables it: find a `PyList_GET_ITEM` / `PyTuple_GET_ITEM` / `PyDict_GetItem` call, identify the variable it's assigned to, scan forward in the same scope for any call that could execute Python code (any `PyObject_*` call, any method call, `Py_DECREF` on an object with `__del__`), then check if the borrowed variable is used after that call.

2. **PyModule_AddObject misuse.** The pre-3.10 semantics steal on success but not on failure, so the caller must handle both. Detect `PyModule_AddObject` calls and check: is there error handling? Does the error path DECREF?  Suggest migration to `PyModule_AddObjectRef`.

3. **Missing DECREF in early-return error paths.** Extensions tend to use inline `return NULL;` instead of CPython's `goto error` pattern. Tree-sitter can find all return statements in a function and check whether locally-owned references are released before each one.

4. **Py_DECREF on potentially-NULL.** Extensions often forget `Py_XDECREF` for variables that may not have been assigned yet (early error before the assignment). Tree-sitter can check: was the variable definitely assigned on all paths reaching this DECREF?

**Script outputs:** Same JSON schema as cpython-review-toolkit's `scan_refcounts.py`, plus:
- `findings[].type: "borrowed_ref_across_call"` (new)
- `findings[].intervening_call` (the call that could invalidate the borrowed ref)

#### 3.2.2 error-path-analyzer (rewritten for extensions)

**Extension-specific patterns:**

1. **Exception clobbering.** Extension code detects an error from API call A, then calls API call B before returning. If B also fails (or succeeds and clears the error), the original exception is lost. Tree-sitter can detect: error check → another PyObject API call → return.

2. **Missing `PyErr_SetString` before return -1 or return NULL.** Extensions sometimes return an error indicator without setting an exception, causing `SystemError: error return without exception set`.

3. **Calling PyErr_Occurred() after a function that sets the exception itself.** Redundant and indicates the author doesn't understand which APIs set exceptions and which use return values only.

4. **Silent exception swallowing.** `PyErr_Clear()` without a clear reason (not inside a "try this, fallback to that" pattern). Especially dangerous in `__del__` / `tp_dealloc` implementations where exceptions must be saved and restored.

#### 3.2.3 module-state-checker (new)

**What it finds:**

1. **Legacy single-phase initialization.** `PyInit_xxx` that calls `PyModule_Create` and returns directly, instead of using `PyModuleDef_Slot` with `Py_mod_exec`. Flag with migration guidance.

2. **Global mutable state.** `static PyObject *` declarations at file scope that aren't `const`. These break subinterpreters and per-interpreter state. Tree-sitter identifies file-scope `static` declarations and classifies them:
   - `static const char *` — acceptable (immutable)
   - `static PyObject *` — flag as global state
   - `static int some_flag` — flag if modified outside init
   - `static PyTypeObject FooType` — flag (should be heap type for multi-phase init)

3. **Module state without lifecycle methods.** `PyModuleDef` with `m_size > 0` but missing `m_traverse`, `m_clear`, or `m_free`. Any `PyObject *` in module state must be traversed for GC.

4. **Incorrect module state access.** Using `PyModule_GetState()` without checking for NULL return (module could be in an error state).

**Script:** `scan_module_state.py`
- Uses Tree-sitter to find `PyModuleDef` struct initializers and `PyInit_*` functions
- Classifies init pattern as single-phase or multi-phase
- Enumerates file-scope static declarations
- Checks `m_size`, `m_traverse`, `m_clear`, `m_free` fields in `PyModuleDef`

#### 3.2.4 type-slot-checker (new)

**What it finds:**

1. **tp_dealloc issues:**
   - Doesn't call `tp_free` (memory leak)
   - Calls `PyObject_Del` instead of `Py_TYPE(self)->tp_free((PyObject *)self)` (breaks inheritance)
   - Doesn't call `PyObject_GC_UnTrack` before clearing members (GC could see half-cleared object)
   - For heap types: missing `Py_DECREF(Py_TYPE(self))` at the end

2. **tp_traverse gaps:** Compare `PyObject *` members in the struct definition against what `tp_traverse` visits. Tree-sitter parses the struct to find all `PyObject *` fields, then checks that `tp_traverse` calls `Py_VISIT` on each one.

3. **tp_richcompare issues:**
   - Returns `Py_NotImplemented` without `Py_INCREF` (returns a borrowed ref as if it were new)
   - Doesn't handle all 6 comparison ops (Py_LT through Py_GE), missing default `Py_RETURN_NOTIMPLEMENTED`

4. **PyType_Spec issues (modern extensions):**
   - Missing `Py_TPFLAGS_DEFAULT`
   - Missing `Py_TPFLAGS_HAVE_GC` when the type has traversable members
   - Slot list that doesn't end with `{0, NULL}`

5. **nb_* / sq_* slot issues:**
   - Number protocol slots that don't handle `Py_RETURN_NOTIMPLEMENTED` for unrecognized types
   - Sequence protocol slots returning wrong types

**Script:** `scan_type_slots.py`
- Uses Tree-sitter to find `PyTypeObject` static structs and `PyType_Spec` definitions
- Parses slot assignments and cross-references with function implementations
- Finds the C struct definition for each type and enumerates `PyObject *` members

#### 3.2.5 gil-discipline-checker (rewritten for extensions)

**Extension-specific patterns beyond cpython-review-toolkit's version:**

1. **Foreign library calls with GIL held.** Extensions wrapping C libraries (OpenSSL, libcurl, SQLite, etc.) often call blocking library functions without releasing the GIL. The agent should flag calls to non-Python functions inside functions that don't have `Py_BEGIN_ALLOW_THREADS`.

2. **Callback from foreign library without GIL.** The inverse: a callback function (registered with a foreign library) that calls Python APIs without first acquiring the GIL via `PyGILState_Ensure`. Pattern: function that is passed as a function pointer to a foreign API, and inside that function calls `PyObject_*` without `PyGILState_Ensure`.

3. **PyGILState during interpreter finalization.** `PyGILState_Ensure` is undefined behavior if the interpreter is finalizing. Extensions with callback-based libraries should check `Py_IsInitialized()` before acquiring the GIL.

4. **Free-threading readiness (PEP 703).** For extensions targeting Python 3.13+: check for `Py_mod_gil` slot, flag thread-unsafe patterns (unprotected static mutable state, non-atomic shared counters), check for `Py_TPFLAGS_ITEMS_AT_END`.

#### 3.2.6 stable-abi-checker (qualitative, new)

**What it finds:**

1. **Internal struct access.** Code that accesses struct members directly (e.g., `op->ob_type`, `tuple->ob_item[i]`, `((PyListObject *)op)->ob_item`) instead of using accessor functions (`Py_TYPE(op)`, `PyTuple_GetItem`, `PyList_GetItem`). These break when struct layouts change.

2. **Private API usage.** Calls to `_Py*` prefixed functions (e.g., `_PyObject_GC_TRACK`, `_PyLong_IsPositive`). These are not part of any stable API.

3. **Wrong headers.** `#include` of `cpython/*.h` or `internal/*.h` headers, which are not available to extensions using the limited API.

4. **Py_LIMITED_API consistency.** If `Py_LIMITED_API` is defined, verify the value matches the minimum Python version the extension claims to support, and that no non-limited-API calls are made.

**No script — qualitative analysis** using Grep and file reading, guided by `data/stable_abi.json` and `data/limited_api_headers.json`.

#### 3.2.7 version-compat-scanner (qualitative, new)

**What it finds:**

1. **API calls without version guards.** Using `PyType_FromModuleAndSpec` (3.10+) or `PyMember_GetOne` replacements without `#if PY_VERSION_HEX >= 0x030A0000` guards.

2. **Removed API usage.** Calling APIs that have been removed in recent Python versions without a version-gated fallback.

3. **Unnecessary compatibility shims.** `#if PY_VERSION_HEX < 0x03080000` blocks when the project requires Python 3.10+. Dead code that complicates maintenance.

4. **pythoncapi-compat opportunities.** Patterns that could be simplified by using the `pythoncapi-compat` header (`pythoncapi_compat.h`), which provides forward-compatible macros.

**No script — qualitative analysis** using Grep and file reading, guided by `data/deprecated_apis.json`.

#### 3.2.8 git-history-analyzer (simplified from code-review-toolkit)

**Capabilities retained:**
- Similar bug detection (the crown jewel)
- Churn × quality risk matrix for prioritization

**Capabilities dropped:**
- Co-change coupling analysis (extensions are typically a handful of files)
- New feature review (not relevant for API-correctness focus)
- Fix completeness review (retained only if the extension has 10+ recent fix commits; otherwise skipped)

**Script:** `analyze_history.py` — adapted from code-review-toolkit's version with the following changes:
- Function boundary detection uses Tree-sitter for C files (not Python AST)
- Classification keywords tuned for extension development patterns
- No timeout on function-level analysis for small extensions (< 50 files)


## 4. Scripts

### 4.1 Script Overview

| Script | Purpose | Tree-sitter? | Adapted From |
|--------|---------|-------------|--------------|
| `tree_sitter_utils.py` | Shared parsing utilities | Core module | New |
| `scan_refcounts.py` | Reference counting analysis | Yes | cpython-review-toolkit |
| `scan_error_paths.py` | Error handling analysis | Yes | cpython-review-toolkit |
| `scan_null_checks.py` | NULL safety analysis | Yes | cpython-review-toolkit |
| `scan_gil_usage.py` | GIL discipline analysis | Yes | cpython-review-toolkit |
| `scan_module_state.py` | Module init and state analysis | Yes | New |
| `scan_type_slots.py` | Type definition analysis | Yes | New |
| `measure_c_complexity.py` | Function complexity scoring | Yes | cpython-review-toolkit |
| `analyze_history.py` | Git history analysis | Yes (for C function boundaries) | code-review-toolkit |
| `discover_extension.py` | Project layout detection | No (reads config files) | New |

### 4.2 Script Output Schema

All scripts output JSON to stdout with a common envelope:

```json
{
  "project_root": "/path/to/project",
  "scan_root": "/path/to/scanned/scope",
  "extension_info": {
    "module_name": "myext",
    "init_style": "multi_phase",
    "python_targets": ">=3.9",
    "limited_api": false,
    "source_files": ["src/myext.c", "src/myext_util.c"],
    "tree_sitter_available": true,
    "clang_tidy_available": false,
    "cppcheck_available": false
  },
  "functions_analyzed": 42,
  "findings": [...],
  "summary": {...}
}
```

### 4.3 Dependency Check

Every script begins with:

```python
try:
    import tree_sitter
    import tree_sitter_c
except ImportError:
    print(json.dumps({
        "error": "tree-sitter not installed",
        "install": "pip install tree-sitter tree-sitter-c",
    }))
    sys.exit(1)
```


## 5. Commands

4 commands, mirroring cpython-review-toolkit's structure but adapted for extensions.

### 5.1 explore

```
/cext-review-toolkit:explore [scope] [aspects] [options]
```

**Aspects:** `refcounts`, `errors`, `null-safety`, `gil`, `module-state`, `type-slots`, `abi`, `compat`, `complexity`, `history`, `all`

**Phases:**

| Phase | Agents | Purpose |
|-------|--------|---------|
| **0** | Extension discovery | Detect extension layout, source files, Python targets |
| **1** | git-history-context (if git repo) | Temporal context for all other agents |
| **2A** | refcount-auditor, error-path-analyzer | Safety-critical (highest value) |
| **2B** | null-safety-scanner, gil-discipline-checker | Memory safety |
| **2C** | module-state-checker, type-slot-checker | Extension correctness |
| **2D** | stable-abi-checker, version-compat-scanner | Compatibility |
| **2E** | measure-c-complexity | Code quality |
| **2F** | git-history-analyzer | Similar bugs, prioritization |
| **3** | Synthesis | Deduplicate, resolve conflicts, produce summary |

Note: No include-graph-mapper phase. Extensions don't have the layered header structure of CPython. Discovery in Phase 0 replaces this structural context.

### 5.2 health

```
/cext-review-toolkit:health [scope]
```

Quick dashboard — all agents in summary mode.

| Dimension | Agent |
|-----------|-------|
| Refcount Safety | refcount-auditor |
| Error Handling | error-path-analyzer |
| NULL Safety | null-safety-scanner |
| GIL Discipline | gil-discipline-checker |
| Module State | module-state-checker |
| Type Slots | type-slot-checker |
| ABI Compliance | stable-abi-checker |
| Version Compat | version-compat-scanner |
| Complexity | c-complexity-analyzer |

### 5.3 hotspots

```
/cext-review-toolkit:hotspots [scope]
```

Runs refcount-auditor, error-path-analyzer, and c-complexity-analyzer. Answers "where should I focus?"

### 5.4 migrate

```
/cext-review-toolkit:migrate [scope]
```

**New command — not in cpython-review-toolkit.** Runs module-state-checker, stable-abi-checker, and version-compat-scanner. Answers "what do I need to change to modernize this extension?" Produces a migration checklist:

```markdown
# Extension Migration Report

## Current State
- Init style: single-phase (legacy)
- Global state: 3 static PyObject* variables
- Stable ABI: not used
- Minimum Python: 3.8

## Migration Checklist

### Phase 1: Multi-phase init (required for subinterpreter support)
- [ ] Convert PyInit_xxx to use PyModuleDef_Slot
- [ ] Move 3 global PyObject* to module state struct
- [ ] Add m_traverse, m_clear, m_free to PyModuleDef
- [ ] Convert static PyTypeObject to heap types

### Phase 2: Stable ABI (optional, enables binary compatibility)
- [ ] Replace 5 internal struct accesses with accessor functions
- [ ] Remove 2 _Py* private API calls
- [ ] Add Py_LIMITED_API define

### Phase 3: Compatibility cleanup
- [ ] Remove 3 dead version guards for Python < 3.8
- [ ] Consider pythoncapi-compat for forward-compatible macros
```


## 6. Classification System

Same as cpython-review-toolkit, with extension-specific calibration:

| Tag | Extension Context | Example |
|-----|------------------|---------|
| **FIX** | Bug that causes crashes, leaks, or wrong behavior | Borrowed ref used after Python callback, missing DECREF on error |
| **CONSIDER** | Likely improvement, may have migration cost | Single-phase init in a working extension, missing `Py_TPFLAGS_BASETYPE` |
| **POLICY** | Design decision for the maintainer | Whether to adopt stable ABI, whether to drop Python 3.8 support |
| **ACCEPTABLE** | Noted but no action needed | Intentional global state for a singleton module, compatible-but-deprecated API usage in a version-gated block |

**Extension-specific calibration rule:** Module state and init style issues are CONSIDER (not FIX) because single-phase init works correctly for the common case — it only breaks with subinterpreters. Type slot issues where the type works but would break under inheritance are also CONSIDER. Only things that cause observable bugs in normal usage are FIX.


## 7. Implementation Plan

### Phase 1: Skeleton + Tree-sitter foundation (Week 1)

- [ ] Project scaffolding: plugin.json, marketplace.json, README, LICENSE, CHANGELOG
- [ ] `tree_sitter_utils.py` — shared parsing module with all utility functions
- [ ] `discover_extension.py` — project discovery script
- [ ] `data/api_tables.json` — vendored from cpython-review-toolkit
- [ ] Test helper adapted for extension project layouts
- [ ] Tests for tree_sitter_utils and discover_extension

### Phase 2: Safety-critical agents (Week 2)

- [ ] `scan_refcounts.py` — Tree-sitter-based, with borrowed-ref-across-call detection
- [ ] `scan_error_paths.py` — Tree-sitter-based, with exception clobbering detection
- [ ] refcount-auditor agent prompt
- [ ] error-path-analyzer agent prompt
- [ ] Tests for both scripts

### Phase 3: Extension-specific agents (Week 3)

- [ ] `scan_module_state.py` — module init and state analysis
- [ ] `scan_type_slots.py` — type definition analysis
- [ ] module-state-checker agent prompt
- [ ] type-slot-checker agent prompt
- [ ] Tests for both scripts

### Phase 4: Remaining agents + commands (Week 4)

- [ ] `scan_null_checks.py` — adapted from cpython-review-toolkit
- [ ] `scan_gil_usage.py` — adapted with extension patterns
- [ ] `measure_c_complexity.py` — Tree-sitter-based adaptation
- [ ] null-safety-scanner, gil-discipline-checker agent prompts
- [ ] stable-abi-checker, version-compat-scanner agent prompts (qualitative)
- [ ] `data/stable_abi.json`, `data/deprecated_apis.json`, `data/limited_api_headers.json`
- [ ] All 4 command definitions (explore, health, hotspots, migrate)

### Phase 5: History + integration (Week 5)

- [ ] `analyze_history.py` — adapted with Tree-sitter C function detection
- [ ] git-history-analyzer agent prompt (simplified)
- [ ] End-to-end test on a real C extension (e.g., coverage.py CTracer)
- [ ] External tool integration (clang-tidy/cppcheck opportunistic)
- [ ] README, plugin README, marketplace listing
- [ ] Final test pass


## 8. Plugin Structure

```
cext-review-toolkit/
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json
├── README.md
├── LICENSE
├── CHANGELOG.md
├── plugins/
│   └── cext-review-toolkit/
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── README.md
│       ├── agents/
│       │   ├── refcount-auditor.md
│       │   ├── error-path-analyzer.md
│       │   ├── null-safety-scanner.md
│       │   ├── gil-discipline-checker.md
│       │   ├── module-state-checker.md
│       │   ├── type-slot-checker.md
│       │   ├── stable-abi-checker.md
│       │   ├── version-compat-scanner.md
│       │   ├── git-history-analyzer.md
│       │   └── c-complexity-analyzer.md
│       ├── commands/
│       │   ├── explore.md
│       │   ├── health.md
│       │   ├── hotspots.md
│       │   └── migrate.md
│       ├── scripts/
│       │   ├── tree_sitter_utils.py
│       │   ├── discover_extension.py
│       │   ├── scan_refcounts.py
│       │   ├── scan_error_paths.py
│       │   ├── scan_null_checks.py
│       │   ├── scan_gil_usage.py
│       │   ├── scan_module_state.py
│       │   ├── scan_type_slots.py
│       │   ├── measure_c_complexity.py
│       │   └── analyze_history.py
│       └── data/
│           ├── api_tables.json
│           ├── deprecated_apis.json
│           ├── stable_abi.json
│           └── limited_api_headers.json
├── tests/
│   ├── helpers.py
│   ├── test_tree_sitter_utils.py
│   ├── test_discover_extension.py
│   ├── test_scan_refcounts.py
│   ├── test_scan_error_paths.py
│   ├── test_scan_null_checks.py
│   ├── test_scan_gil_usage.py
│   ├── test_scan_module_state.py
│   ├── test_scan_type_slots.py
│   ├── test_measure_c_complexity.py
│   └── test_analyze_history.py
└── .gitignore
```


## 9. Testing Strategy

### 9.1 Test Helper

Adapted from cpython-review-toolkit's `TempProject`, with extension-aware fixtures:

```python
class TempExtension:
    """Create a temporary C extension project for testing."""

    def __init__(self, files: dict[str, str], *, setup_py: str | None = None):
        # Creates temp dir with given C files
        # Optionally creates setup.py with ext_modules
        # Always creates a minimal Python.h stub for parsing
        ...
```

### 9.2 Test Coverage Requirements

Each script must have tests covering:
- Detection of the primary bug pattern (true positive)
- Clean code producing no findings (true negative)
- At least one edge case (macro-heavy code, unconventional formatting)

### 9.3 Real-world Validation

Before release, run on at least 3 real C extensions:
- coverage.py CTracer (already validated with cpython-review-toolkit)
- A scientific computing extension (e.g., a small NumPy-adjacent project)
- A database wrapper (e.g., a SQLite or Redis extension)


## 10. Differences from cpython-review-toolkit (Summary)

| Dimension | cpython-review-toolkit | cext-review-toolkit |
|-----------|----------------------|-------------------|
| Parsing | Regex (PEP 7 regularity) | Tree-sitter (style-agnostic) |
| Dependencies | stdlib only | tree-sitter, tree-sitter-c |
| Project discovery | Include/Python.h + Objects/object.c | setup.py, pyproject.toml, meson.build, or #include Python.h |
| Perspective | Code that implements the C API | Code that consumes the C API |
| Agents | 10 | 10 (8 analysis + 1 complexity + 1 history) |
| Unique agents | include-graph-mapper, pep7-style-checker, api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer | module-state-checker, type-slot-checker, stable-abi-checker, version-compat-scanner |
| Unique command | map | migrate |
| Borrowed ref analysis | Flags missing INCREF in CPython code | Flags borrowed refs held across Python callbacks (Tree-sitter-enabled) |
| Module state | N/A (CPython manages its own state) | Core concern — single-phase vs multi-phase, global state |
| External tools | None | Optional clang-tidy, cppcheck |
| Style checking | PEP 7 | None (extensions follow their own style) |
