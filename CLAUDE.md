# CLAUDE.md — cext-review-toolkit development guide

## Project overview
cext-review-toolkit is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for reviewing CPython C extensions. It finds API misuse, memory safety bugs, compatibility issues, and correctness problems in code that *consumes* the Python/C API.

Part of a family of review toolkits:
- [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit) — Python source code
- [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) — CPython runtime C code
- **cext-review-toolkit** — C extensions (this project)

Key architectural difference: uses Tree-sitter for C parsing (not regex), enabling analysis that regex cannot do (borrowed-ref lifetime tracking, type slot cross-referencing, accurate scope analysis).

## Prerequisites
- Python 3.10+
- `tree-sitter` and `tree-sitter-c`: `pip install tree-sitter tree-sitter-c`
- No other dependencies — all scripts use only the standard library plus tree-sitter

## Dev commands
```bash
# Run all tests
python -m unittest discover tests -v

# Run a specific test file
python -m unittest tests.test_scan_refcounts -v

# Run a single script standalone (all output JSON to stdout)
python plugins/cext-review-toolkit/scripts/scan_refcounts.py /path/to/extension.c
python plugins/cext-review-toolkit/scripts/discover_extension.py /path/to/project
```

## Code style
- Python 3.10+ (uses `X | Y` union syntax, `match` statements)
- Double quotes for strings
- Type hints on all function signatures
- Docstrings on classes and public functions
- Tests use `unittest` — never pytest
- Linted and formatted with ruff, type checked with mypy

## Project structure

This is a Claude Code plugin, not a pip-installable package.

```
cext-review-toolkit/
├── CLAUDE.md                          # This file
├── README.md                          # User-facing documentation
├── CHANGELOG.md                       # Keep a Changelog format
├── LICENSE                            # MIT
├── cext-review-toolkit-design.md      # Full design document (authoritative reference)
├── .claude/skills/task-workflow/      # Claude Code skill for dev workflow
├── plugins/cext-review-toolkit/       # The actual plugin
│   ├── .claude-plugin/plugin.json     # Plugin metadata
│   ├── agents/                        # 10 agent prompt definitions (markdown)
│   ├── commands/                      # 4 command definitions (markdown)
│   ├── scripts/                       # 11 Python scripts (the core code)
│   └── data/                          # 4 JSON data files (API tables, etc.)
└── tests/                             # unittest test suite
```

## Architecture

### Scripts (the core analysis code)

All scripts live in `plugins/cext-review-toolkit/scripts/`. Every analysis script follows the same pattern: parse C files with Tree-sitter, find candidate issues, output JSON to stdout.

| Script | Lines | Purpose |
|--------|-------|---------|
| `tree_sitter_utils.py` | ~550 | Core parsing module — all other scripts import from here |
| `scan_common.py` | ~130 | Shared utilities: project root, file discovery, API tables, arg parsing |
| `scan_refcounts.py` | ~360 | Reference counting errors (leaked refs, borrowed-ref-across-call, stolen-ref misuse) |
| `scan_error_paths.py` | ~330 | Error handling bugs (missing NULL checks, exception clobbering, return-without-exception) |
| `scan_null_checks.py` | ~250 | NULL safety (unchecked allocations, deref-before-check) |
| `scan_gil_usage.py` | ~300 | GIL discipline (mismatched macros, API without GIL, blocking with GIL, free-threading) |
| `scan_module_state.py` | ~320 | Module init and state (single-phase init, global state, missing traverse) |
| `scan_type_slots.py` | ~430 | Type definitions (dealloc, traverse, richcompare, flags, heap types) |
| `measure_c_complexity.py` | ~250 | Function complexity scoring |
| `analyze_history.py` | ~520 | Git history analysis (similar bugs, churn prioritization) |
| `discover_extension.py` | ~420 | Extension project layout detection |

**Dependency graph:** `tree_sitter_utils.py` is at the center. `scan_common.py` imports from it. All other scripts import from both. No circular dependencies.

**Script calling convention:** Every analysis script exposes `analyze(target: str, *, max_files: int = 0) -> dict` and a `main()` that outputs JSON to stdout. Exception: `analyze_history.py` takes `argv` to match code-review-toolkit conventions.

**Data files** in `plugins/cext-review-toolkit/data/`:
- `api_tables.json` — NEW_REF_APIS, BORROWED_REF_APIS, STEAL_REF_APIS
- `deprecated_apis.json` — deprecated APIs with version and replacement
- `stable_abi.json` — functions in the stable ABI
- `limited_api_headers.json` — headers permitted under Py_LIMITED_API

### Agents (prompt definitions for Claude Code)

10 markdown files in `plugins/cext-review-toolkit/agents/`. Each has YAML frontmatter (name, description, model, color) and a structured prompt telling Claude Code how to use the corresponding script and interpret its output.

Agents don't contain analysis logic — they instruct Claude Code to run a script, read the JSON output, then perform deep qualitative review of each candidate finding. The scripts find candidates (with ~20-40% false positive rate); the agents confirm or dismiss them.

| Agent | Script | Focus |
|-------|--------|-------|
| refcount-auditor | scan_refcounts.py | Borrowed-ref-across-call is the crown jewel finding |
| error-path-analyzer | scan_error_paths.py | Exception clobbering, return-without-exception |
| null-safety-scanner | scan_null_checks.py | Unchecked allocations, deref-before-check |
| gil-discipline-checker | scan_gil_usage.py | Callback-without-GIL, free-threading readiness |
| module-state-checker | scan_module_state.py | Single-phase init, global state migration |
| type-slot-checker | scan_type_slots.py | Dealloc, traverse, richcompare correctness |
| stable-abi-checker | (qualitative) | Grep-based, uses data/stable_abi.json |
| version-compat-scanner | (qualitative) | Grep-based, uses data/deprecated_apis.json |
| git-history-analyzer | analyze_history.py | Similar bug detection, churn prioritization |
| c-complexity-analyzer | measure_c_complexity.py | Function complexity scoring |

### Commands (orchestration)

4 markdown files in `plugins/cext-review-toolkit/commands/`:
- `explore.md` — primary command, runs agents in phased groups
- `health.md` — quick scored dashboard, all agents in summary mode
- `hotspots.md` — refcount + errors + complexity, find worst functions
- `migrate.md` — modernization checklist (multi-phase init, stable ABI, compat)

### Classification system

Every finding is tagged:
- **FIX** — bug causing crashes, leaks, or wrong behavior
- **CONSIDER** — likely improvement, may have migration cost
- **POLICY** — design decision for the maintainer
- **ACCEPTABLE** — noted but no action needed

Important calibration: module state issues (single-phase init, global state) are CONSIDER, not FIX — they work correctly, they just limit subinterpreter support.

## Testing notes
- All tests use `unittest` — never pytest
- Test helper in `tests/helpers.py`: `TempExtension` context manager, `import_script()` loader
- 4 C code fixtures: MINIMAL_EXTENSION, MULTI_PHASE_EXTENSION, EXTENSION_WITH_TYPE, EXTENSION_WITH_BUGS
- 1 setup.py template: SETUP_PY_TEMPLATE
- Tests create temporary directories with C files, run scripts on them, and check JSON output
- `import_script(name)` loads scripts from `plugins/cext-review-toolkit/scripts/` via importlib

## Adding a new analysis script

1. Create `plugins/cext-review-toolkit/scripts/scan_newcheck.py`
2. Import from `tree_sitter_utils` and `scan_common`
3. Implement `analyze(target: str, *, max_files: int = 0) -> dict` following the common JSON envelope
4. Add `main()` using `parse_common_args()` from `scan_common`
5. Create `tests/test_scan_newcheck.py` with at least: true positive, true negative, edge case
6. Create `plugins/cext-review-toolkit/agents/newcheck-agent.md` with YAML frontmatter and prompt
7. Add the agent to the appropriate phase group in `commands/explore.md`
8. Update CHANGELOG.md

## Adding a new agent (qualitative, no script)

1. Create `plugins/cext-review-toolkit/agents/new-agent.md` with YAML frontmatter
2. The agent prompt should instruct Claude Code to use Grep/Read tools directly
3. Reference data files in `data/` for API lists and version information
4. Add to the appropriate phase group in `commands/explore.md`
5. Update CHANGELOG.md

## Design document

`cext-review-toolkit-design.md` at the repo root is the authoritative design reference. It covers: project identity, architecture decisions (why Tree-sitter, why separate from cpython-review-toolkit), all agent specifications, script output schemas, command definitions, classification system, and implementation plan.

## Workflow
- Use `/task-workflow <description>` for the full issue → branch → code → test → commit → PR → merge cycle
