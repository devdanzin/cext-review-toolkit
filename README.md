# C Extension Review Toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for reviewing CPython C extensions -- finding API misuse, memory safety bugs, compatibility issues, and correctness problems specific to code that *consumes* the Python/C API.

Built for the specific concerns of C extension authors -- reference counting from the caller's perspective, borrowed reference lifetimes, module state management, type slot correctness, stable ABI compliance, and version compatibility -- not general-purpose C analysis.

## Why a Separate Tool?

| Concern | CPython internals (cpython-review-toolkit) | C extensions (this toolkit) |
|---------|-------------------------------------------|----------------------------|
| **Perspective** | Code that *implements* the C API | Code that *calls* the C API |
| **Parsing** | Regex (PEP 7 code is regular) | Tree-sitter (extension code varies wildly) |
| **Top bug class** | Refcount leaks in runtime code | Borrowed refs held across callbacks |
| **Module state** | N/A (CPython manages its own) | Core concern -- init style, global state |
| **Type definitions** | Part of the runtime | Must follow slot contracts correctly |
| **ABI** | Defines the ABI | Must comply with the ABI |
| **Dependencies** | stdlib only | tree-sitter, tree-sitter-c |

## Installation

### Marketplace install (recommended)

```bash
claude plugin marketplace add devdanzin/cext-review-toolkit
claude plugin install cext-review-toolkit@cext-review-toolkit
```

### Direct install from GitHub

```bash
claude plugin install cext-review-toolkit --source github:devdanzin/cext-review-toolkit --path plugins/cext-review-toolkit
```

### Without installing (try it first)

```bash
git clone https://github.com/devdanzin/cext-review-toolkit.git
claude --plugin-dir cext-review-toolkit/plugins/cext-review-toolkit
```

### Prerequisites

- **Claude Code** installed and running
- **Python 3.10+** for the analysis scripts
- **tree-sitter** and **tree-sitter-c**: `pip install tree-sitter tree-sitter-c`

## Quick Start

Navigate to a C extension project, then:

```bash
/cext-review-toolkit:health       # Quick health dashboard
/cext-review-toolkit:hotspots     # Refcount leaks + error bugs + complexity
/cext-review-toolkit:explore      # Full exploration (all 10 agents)
/cext-review-toolkit:migrate      # Modernization checklist
```

Start with `health` for a quick overview, then `hotspots` to find the highest-impact bugs.

## What's Included

### Agents

#### Safety-Critical (script-backed, Tree-sitter-powered)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **refcount-auditor** | Leaked refs, borrowed-ref-across-callback, stolen-ref misuse, missing Py_CLEAR | `scan_refcounts.py` |
| **error-path-analyzer** | Missing NULL checks, exception clobbering, return-without-exception | `scan_error_paths.py` |
| **null-safety-scanner** | Unchecked allocations, deref-before-check | `scan_null_checks.py` |
| **gil-discipline-checker** | GIL released during Python API, blocking I/O with GIL, callback GIL issues, free-threading readiness | `scan_gil_usage.py` |

#### Extension-Specific (script-backed, Tree-sitter-powered)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **module-state-checker** | Legacy single-phase init, global PyObject* state, missing m_traverse/m_clear, static types | `scan_module_state.py` |
| **type-slot-checker** | Missing tp_free, traverse gaps, wrong Py_NotImplemented handling, heap type issues | `scan_type_slots.py` |

#### Compatibility (qualitative)

| Agent | What It Finds |
|-------|--------------|
| **stable-abi-checker** | Internal struct access, private API calls, limited API violations |
| **version-compat-scanner** | API calls without version guards, dead compatibility code, deprecated APIs |

#### Code Quality and History

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **c-complexity-analyzer** | Functions scored by complexity, nesting, line count | `measure_c_complexity.py` |
| **git-history-analyzer** | Similar bugs elsewhere, churn-based risk prioritization | `analyze_history.py` |

### Commands

| Command | Purpose | Agents Used |
|---------|---------|-------------|
| `explore` | Full analysis with selectable aspects | All (configurable) |
| `health` | Quick scored dashboard | All in summary mode |
| `hotspots` | Find worst functions to fix first | refcount + errors + complexity |
| `migrate` | Modernization checklist | module-state + type-slots + abi + compat |

## How It Works

### Tree-sitter Parsing

Unlike cpython-review-toolkit (regex-based), this toolkit uses Tree-sitter for C parsing. This enables analysis that regex fundamentally cannot do:

- **Borrowed reference lifetime tracking**: Detect when a borrowed ref survives across a call back into Python -- the #1 extension-specific bug pattern
- **Type slot cross-referencing**: Connect a PyTypeObject/PyType_Spec to its struct definition and slot function implementations
- **Accurate scope analysis**: Distinguish file-scope static variables from local ones, track variable assignments within functions

### Extension Discovery

The toolkit auto-detects C extensions in diverse project layouts:
- `setup.py` with `ext_modules`
- `pyproject.toml` with setuptools, meson-python, or scikit-build
- `meson.build` with `py.extension_module()`
- `CMakeLists.txt` with pybind11 or Python3 library targets
- Fallback: any `.c` file containing `#include <Python.h>`

### Classification System

Every finding is tagged:

| Tag | Meaning | Example |
|-----|---------|---------|
| **FIX** | Bug causing crashes, leaks, or wrong behavior | Borrowed ref across callback, missing DECREF |
| **CONSIDER** | Likely improvement, may have migration cost | Single-phase init, missing Py_TPFLAGS_BASETYPE |
| **POLICY** | Design decision for the maintainer | Whether to adopt stable ABI, drop old Python versions |
| **ACCEPTABLE** | Noted but no action needed | Intentional global state for singleton module |

### External Tool Integration (Optional)

If available, the toolkit can use:
- **clang-tidy** with `compile_commands.json` for deeper data-flow analysis
- **cppcheck** for buffer overflow and uninitialized variable detection

These are never required -- the Tree-sitter-based analysis is the baseline.

## Recommended Workflows

### Reviewing an unfamiliar extension

```
1. /cext-review-toolkit:health              -> Quick overview
2. /cext-review-toolkit:hotspots            -> Where are the bugs?
3. /cext-review-toolkit:explore . refcounts errors deep  -> Deep dive on safety
```

### Preparing for a Python version upgrade

```
1. /cext-review-toolkit:explore . compat abi  -> What needs to change?
2. /cext-review-toolkit:migrate               -> Full migration checklist
```

### Modernizing an extension

```
1. /cext-review-toolkit:migrate               -> What to modernize
2. /cext-review-toolkit:explore . module-state type-slots deep  -> Detailed guidance
```

### Pre-release safety audit

```
1. /cext-review-toolkit:explore . all deep    -> Full analysis
2. Focus on FIX findings
3. Re-run on specific files after fixes
```

## Explore Command Phases

| Phase | Agents | Purpose |
|-------|--------|---------|
| **0** | Extension discovery | Detect layout, source files, Python targets |
| **1** | (git history noted) | Temporal context available for Phase 2F |
| **2A** | refcount-auditor, error-path-analyzer | Safety-critical (highest value) |
| **2B** | null-safety-scanner, gil-discipline-checker | Memory safety |
| **2C** | module-state-checker, type-slot-checker | Extension correctness |
| **2D** | stable-abi-checker, version-compat-scanner | Compatibility |
| **2E** | c-complexity-analyzer | Code quality |
| **2F** | git-history-analyzer | Similar bugs, risk prioritization |
| **3** | Synthesis | Deduplicate, resolve conflicts, produce summary |

## Limitations

- **Tree-sitter, not a compiler**: Cannot resolve macros, follow pointer aliasing, or track through complex preprocessor conditionals. Reports candidates with expected 20-40% false positive rate; agents confirm or dismiss each finding.
- **Single-file scope for scripts**: Scripts analyze each function independently. Cross-function reference ownership transfer is tracked only at the API boundary level.
- **External tools are optional**: Without `compile_commands.json`, no clang-tidy or cppcheck integration. The Tree-sitter analysis is comprehensive but has inherent limits.
- **Struct-to-type matching is heuristic**: Connecting a PyTypeObject to its backing struct uses name matching and `tp_basicsize` analysis. Unusual naming or indirection may cause mismatches.

## Comparison with Sibling Projects

| Dimension | code-review-toolkit | cpython-review-toolkit | cext-review-toolkit |
|-----------|--------------------|-----------------------|--------------------|
| **Language** | Python | C (CPython source) | C (extensions) |
| **Parsing** | Python `ast` | Regex | Tree-sitter |
| **Target** | Python projects | CPython runtime | C extensions |
| **Agents** | 14 | 10 | 10 |
| **Scripts** | 8 | 7 | 10 |
| **Unique value** | Test coverage, architecture | GIL, PEP 7, include graph | Module state, type slots, ABI, migrate |

## Author

Danzin

## License

MIT -- see [LICENSE](LICENSE) for details.
