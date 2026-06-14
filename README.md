# C Extension Review Toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for reviewing CPython C extensions — finding API misuse, memory safety bugs, compatibility issues, and correctness problems specific to code that *consumes* the Python/C API.

Built for the specific concerns of C extension authors — reference counting from the caller's perspective, borrowed-reference lifetimes, module state management, type slot correctness, free-threading readiness, parity between C and Python implementations, stable ABI compliance, and version compatibility — not general-purpose C analysis.

---

## ⚠️ Read this before you use the toolkit on someone else's project

This tool finds bugs in **other people's code**. That code is usually written by someone giving away their work for free. Before running this on a project you don't own:

- **Read [WORKING_WITH_MAINTAINERS.md](WORKING_WITH_MAINTAINERS.md).** It is the most important document in this repository.
- **Reach out to the maintainer first.** A short, friendly message — *"I'd like to run a static-analysis tool on your project, would the report be useful?"* — takes five minutes and changes everything that follows.
- **Don't auto-file issues. Don't auto-open PRs.** Every finding should pass through human triage before reaching the maintainer.
- **Security findings need responsible disclosure**, not a public issue.

A 50-finding report is 50 hours of homework for a maintainer who didn't ask for it. The tool's value depends entirely on whether they want to receive what you produce.

---

## Why a Separate Tool?

| Concern | CPython internals (cpython-review-toolkit) | C extensions (this toolkit) |
|---------|-------------------------------------------|----------------------------|
| **Perspective** | Code that *implements* the C API | Code that *calls* the C API |
| **Parsing** | Regex (PEP 7 code is regular) | Tree-sitter (extension code varies wildly) |
| **Top bug class** | Refcount leaks in runtime code | Borrowed refs held across callbacks; UAF in proxy callbacks |
| **Module state** | N/A (CPython manages its own) | Core concern — init style, global state, sub-interpreter compat |
| **Type definitions** | Part of the runtime | Must follow slot contracts correctly |
| **ABI** | Defines the ABI | Must comply with the ABI |
| **Dependencies** | stdlib only | tree-sitter, tree-sitter-c (optional: tree-sitter-cpp, tree-sitter-cython) |

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
- **tree-sitter-cpp** (optional): `pip install tree-sitter-cpp` — enables C++ file parsing
- **tree-sitter-cython** (optional, for Cython extensions): `pip install git+https://github.com/devdanzin/tree-sitter-cython.git@fix/grammar-gaps`
- **clang-tidy** and **cppcheck** (optional): system packages — enables external tool cross-referencing
- **libfiu** (optional): for OOM-injection reproducers

## Quick Start

Navigate to a C extension project, then:

```bash
/cext-review-toolkit:health       # Quick health dashboard
/cext-review-toolkit:hotspots     # Refcount leaks + error bugs + complexity
/cext-review-toolkit:explore      # Full exploration (mapper preflight + 13 audit agents + reproducer pass)
/cext-review-toolkit:migrate      # Modernization checklist
```

Start with `health` for a quick overview, then `hotspots` to find the highest-impact bugs. Use `explore` for the full audit pipeline.

### Cost and time expectations

A full `/explore` on a medium extension (~5 KLOC) typically runs:

- **~10-30 minutes** wall-clock for the audit phase
- **Real money** in Claude API tokens — a thorough multi-pass review can cost anywhere from a few dollars to tens of dollars depending on extension size, agent count, and whether you run the reproducer pass
- **Significantly more** for large extensions (~25 KLOC+) or when the 2-naive + 1-informed methodology is run end-to-end

This isn't free to run, and it isn't free for the maintainer to receive. Plan accordingly.

## What's Included

### Agents (13 audit + 1 phase-0 specialist)

#### Phase-0 specialist

| Agent | What It Does |
|-------|--------------|
| **generated-code-mapper** | Detects code generators (Cython, pybind11, nanobind, custom) and produces a per-file orientation guide (`generated_code_map.md`) that downstream agents read. Mandatory first step on every review. |

#### Safety-critical (script-backed, Tree-sitter-powered)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **refcount-auditor** | Leaked refs, borrowed-ref-across-callback, stolen-ref misuse, missing Py_CLEAR, UAF in proxy callbacks | `scan_refcounts.py` |
| **error-path-analyzer** | Missing NULL checks, exception clobbering, return-without-exception, error-buffer races | `scan_error_paths.py` |
| **null-safety-scanner** | Unchecked allocations, deref-before-check, user-driven state-machine NULL | `scan_null_checks.py` |
| **gil-discipline-checker** | GIL released during Python API, blocking I/O with GIL, callback GIL issues, free-threading readiness | `scan_gil_usage.py` |
| **resource-lifecycle-checker** | Non-PyObject resource pairs, talloc lifetime, libuv handle leaks, FD-window leaks, buffer protocol | `scan_resource_lifecycle.py` |

#### Extension-specific (script-backed, Tree-sitter-powered)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **module-state-checker** | Legacy single-phase init, global PyObject* state, missing m_traverse/m_clear, static types, sub-interpreter readiness | `scan_module_state.py` |
| **type-slot-checker** | Missing tp_free, traverse gaps, GC-flag inconsistency, re-init leak, heap type issues | `scan_type_slots.py` |
| **pyerr-clear-auditor** | Silent MemoryError/KeyboardInterrupt swallowing, error-rewrite without `PyErr_ExceptionMatches` | `scan_pyerr_clear.py` |

#### Compatibility and parity

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **stable-abi-checker** | Internal struct access, private API calls, limited API violations, abi3 feasibility | (qualitative + `data/stable_abi.json`) |
| **version-compat-scanner** | API calls without version guards, dead compatibility code, deprecated APIs, `pythoncapi-compat` opportunities | `scan_version_compat.py` |
| **parity-checker** | Behavioral differences between C and Python implementations of the same functionality; for Cython, `.pyi` ↔ `.pyx` adapted-scope checks | (qualitative) |

#### Code quality and history

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **c-complexity-analyzer** | Functions scored by complexity, nesting, line count; cross-referenced with safety findings | `measure_c_complexity.py` |
| **git-history-analyzer** | Similar bugs elsewhere, churn-based risk prioritization, fix-completeness gaps | `analyze_history.py` |

### Commands

| Command | Purpose | Agents Used |
|---------|---------|-------------|
| `explore` | Full analysis with selectable aspects, mapper preflight, optional reproducer pass | All (configurable) |
| `health` | Quick scored dashboard | All in summary mode |
| `hotspots` | Find worst functions to fix first | refcount + errors + complexity |
| `migrate` | Modernization checklist | module-state + type-slots + abi + compat |

## How It Works

### The 2-naive + 1-informed methodology

The standard practice for thorough reviews is:

1. **Phase 0 — Discovery + mapper preflight.** The generated-code-mapper runs first, producing a per-file orientation guide that downstream agents read. External tools (clang-tidy + cppcheck) baseline if available.
2. **R1 — Naive pass 1.** All relevant audit agents dispatched in parallel; each agent reads the mapper preflight, runs its scanner, and triages findings independently.
3. **R2 — Naive pass 2 (independent).** Same agents re-dispatched without R1's findings as context. When R1 and R2 independently flag the same site, that site is highly confirmed.
4. **R3 — Informed-rerun.** Agents dispatched with a synthesized briefing summarizing R1+R2 convergences. Instructed to NOT re-flag confirmed bugs but to hunt for adjacent code, twin-class divergence, and missed paths. Typically adds 5-10 net-new findings R1+R2 missed.
5. **Synthesis.** Builds a convergence matrix across R1/R2/R3 + external tools. Resolves tensions, classifies severity, writes the consolidated report.
6. **Reproducer pass (optional).** Categorizes every FIX/CONSIDER finding into one of 7 tiers (public-API SEGV, libfiu OOM injection, refcount/RSS leak, async/sync I/O, TSan FT race, hand-written, source-only) and dispatches a reproducer agent per tier in parallel. Each reproducer is subprocess-isolated and produces a verifiable artifact.

### Tree-sitter Parsing

Unlike cpython-review-toolkit (regex-based), this toolkit uses Tree-sitter for C parsing. This enables analysis that regex fundamentally cannot do:

- **Borrowed reference lifetime tracking**: detect when a borrowed ref survives across a call back into Python — the #1 extension-specific bug pattern
- **Type slot cross-referencing**: connect a `PyTypeObject` / `PyType_Spec` to its struct definition and slot function implementations
- **Accurate scope analysis**: distinguish file-scope static variables from local ones, track variable assignments within functions

For Cython projects, an additional `tree-sitter-cython` parser layer powers five `.pyx`-aware scanners (silent-noexcept, buffer protocol, PyCapsule, cinit/init reinit-leak, nogil-touches-Python).

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
| **FIX** | Bug causing crashes, leaks, or wrong behavior | Borrowed ref across callback, missing DECREF, NDEBUG-stripped assertion masking NULL |
| **CONSIDER** | Likely improvement, may have migration cost | Single-phase init, missing Py_TPFLAGS_BASETYPE, missing GC support |
| **POLICY** | Design decision for the maintainer — *their call, not yours* | Whether to adopt stable ABI, drop old Python versions, multi-phase init |
| **ACCEPTABLE** | Noted but no action needed | Intentional global state for singleton module |

`POLICY` items are not "things you should push the maintainer to fix"; they're things that need *the maintainer's* judgment. See [WORKING_WITH_MAINTAINERS.md](WORKING_WITH_MAINTAINERS.md) for severity-translation guidance.

### External Tool Integration (Optional)

If available, the toolkit can use:
- **clang-tidy** with `compile_commands.json` for deeper data-flow analysis
- **cppcheck** for buffer overflow and uninitialized variable detection (no `compile_commands.json` required, but its output is more useful with one)

These are never required — the Tree-sitter-based analysis is the baseline.

## Recommended Workflows

### Reviewing an unfamiliar extension you don't own

1. **First, read [WORKING_WITH_MAINTAINERS.md](WORKING_WITH_MAINTAINERS.md).**
2. Skim recent commits, open issues, and `CONTRIBUTING.md` / `SECURITY.md`.
3. `/cext-review-toolkit:health` — quick overview.
4. `/cext-review-toolkit:hotspots` — where are the bugs?
5. Decide whether the maintainer would benefit from a deeper review. If yes, **reach out before going further**.
6. With agreement: `/cext-review-toolkit:explore . refcounts errors deep` for a focused safety pass, or the full `/explore` pipeline.

### Reviewing your own extension

```
1. /cext-review-toolkit:explore . all deep    -> Full analysis
2. Focus on FIX findings; reproduce the highest-impact ones first
3. Re-run on specific files after fixes
```

### Preparing for a Python version upgrade

```
1. /cext-review-toolkit:explore . compat abi  -> What needs to change?
2. /cext-review-toolkit:migrate               -> Full migration checklist
```

### Modernizing an extension you maintain

```
1. /cext-review-toolkit:migrate               -> What to modernize
2. /cext-review-toolkit:explore . module-state type-slots deep  -> Detailed guidance
```

## Explore Command Phases

| Phase | Agents | Purpose |
|-------|--------|---------|
| **0** | Extension discovery, mapper preflight, external tools baseline | Detect layout, source files, generators, codegen idioms; produce orientation guide |
| **1** | refcount-auditor, error-path-analyzer | Safety-critical (highest value) |
| **2** | null-safety-scanner, gil-discipline-checker, resource-lifecycle-checker | Memory safety |
| **3** | module-state-checker, type-slot-checker, pyerr-clear-auditor | Extension correctness |
| **4** | stable-abi-checker, version-compat-scanner, parity-checker | Compatibility and parity |
| **5** | c-complexity-analyzer | Code quality |
| **6** | git-history-analyzer | Similar bugs, risk prioritization, fix-completeness review |
| **7** | Synthesis | Deduplicate, resolve conflicts, produce consolidated report |
| **8** *(optional)* | Reproducer pass | 7-tier dispatch: public-API SEGV, libfiu OOM, refcount/RSS, async/sync I/O, TSan FT race, hand-written, source-only |

For thorough reviews, run R1 → R2 → R3 (the 2-naive + 1-informed methodology) before synthesis.

## Limitations

- **Tree-sitter, not a compiler**: Cannot resolve macros, follow pointer aliasing, or track through complex preprocessor conditionals. Reports candidates with expected 20-40% false positive rate; agents confirm or dismiss each finding.
- **Single-file scope for scripts**: Scripts analyze each function independently. Cross-function reference ownership transfer is tracked only at the API boundary level. Cross-TU analysis is the agent's job, not the script's.
- **External tools are optional**: Without `compile_commands.json`, no clang-tidy. cppcheck still runs but with less data-flow precision.
- **Struct-to-type matching is heuristic**: Connecting a `PyTypeObject` to its backing struct uses name matching and `tp_basicsize` analysis. Unusual naming or indirection may cause mismatches.
- **The tool's confidence is not a substitute for your triage.** Even FIX findings need human review before being shared with a maintainer.

## Documentation

| File | Purpose |
|------|---------|
| **[WORKING_WITH_MAINTAINERS.md](WORKING_WITH_MAINTAINERS.md)** | **How to use what this tool produces in a way that helps maintainers rather than burdens them. Read first if you're using this on someone else's project.** |
| [docs/reproducer-techniques.md](docs/reproducer-techniques.md) | Catalogue of reproducer techniques (32 numbered patterns) |
| [cext-review-toolkit-design.md](cext-review-toolkit-design.md) | Design document — architecture, scripts, agents, classification |

## Comparison with Sibling Projects

| Dimension | code-review-toolkit | cpython-review-toolkit | cext-review-toolkit |
|-----------|--------------------|-----------------------|--------------------|
| **Language** | Python | C (CPython source) | C / C++ / Cython (extensions) |
| **Parsing** | Python `ast` | Regex | Tree-sitter |
| **Target** | Python projects | CPython runtime | C extensions |
| **Audit agents** | 14 | 10 | 13 + 1 phase-0 specialist |
| **Scripts** | 8 | 7 | 15+ |
| **Unique value** | Test coverage, architecture | GIL, PEP 7, include graph | Module state, type slots, ABI, parity, FT readiness, reproducer pass |

There's also a sibling **[ft-review-toolkit](https://github.com/devdanzin/ft-review-toolkit)** focused specifically on free-threading analysis (PEP 703 readiness, TSan triage, race classification) that complements this toolkit on FT-relevant reviews.

## Author

Daniel ([@devdanzin](https://github.com/devdanzin))

## License

MIT — see [LICENSE](LICENSE) for details.

The MIT license disclaims warranty in the legal sense. The social contract — between the tool's user and the open-source maintainer whose code you're reviewing — is not a legal question. See [WORKING_WITH_MAINTAINERS.md](WORKING_WITH_MAINTAINERS.md).
