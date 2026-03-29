# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.3] - 2026-03-29

### Added
- `docs/reproducer-techniques.md`: Technique 19 — stateful metaclass hash for type-keyed dict lookups. Confirmed on pymongo/bson.

### Enhanced
- `type-slot-checker` agent: added immutable-type exception for missing `tp_clear`. Types whose `PyObject*` members are set once during construction and never mutated are now classified as ACCEPTABLE (not CONSIDER) when missing `tp_clear`, matching CPython's own convention.
- `type-slot-checker` agent and `scan_type_slots.py`: added two new checks based on APSW maintainer feedback:
  - `init_not_reinit_safe`: detects `tp_init` that allocates resources without checking/cleaning prior state. Python allows calling `__init__()` multiple times, so a second call leaks the first call's resources.
  - `new_missing_member_init`: detects `tp_new` that uses a non-zeroing allocator without initializing pointer members. Python allows `__new__()` without `__init__()`, so methods may dereference uninitialized pointers.
- `tree_sitter_utils.py`: `find_struct_members` now returns `is_pointer` field for struct member dicts.

## [0.1.2] - 2026-03-25

### Added
- Code generation auto-detection in `discover_extension.py`: identifies Cython, mypyc, pybind11, or hand-written C code.
- `code_generation` field in discovery output enables explore command to skip high-FP agents on generated code.
- Code generation strategy section in `explore.md` with agent dispatch guidance per code type.
- `scan_pyerr_clear.py` script: audits PyErr_Clear() calls for unguarded exception swallowing.
- `pyerr-clear-auditor` agent: qualitative analysis of dangerous PyErr_Clear patterns.
- `pyerr-clear` aspect keyword in explore command for targeted PyErr_Clear auditing.
- `scan_resource_lifecycle.py` script: tracks non-PyObject resource allocation/free pairing (malloc/free, HDF5 handles, buffer protocol, file I/O).
- `resource-lifecycle-checker` agent: qualitative analysis of resource leaks on error paths.
- `resource_pairs.json` data file: configurable allocation/free pairs for lifecycle tracking (C memory, Python memory, HDF5, buffer protocol, file I/O).
- `resources` aspect keyword in explore command for targeted resource lifecycle auditing.
- `parity-checker` agent: finds behavioral differences between C and Python dual implementations.
- `parity` aspect keyword in explore command for C/Python parity analysis.
- C++ file support via optional `tree-sitter-cpp` dependency (.cpp, .cxx, .cc files).
- `run_external_tools.py` script wrapping clang-tidy and cppcheck with JSON envelope output.
- Phase 0.5 in `explore.md` for automatic external tool baseline.
- External Tool Cross-Reference sections in 4 agent prompts (null-safety, error-path, GIL, complexity).
- `parse_bytes_for_file()` in `tree_sitter_utils.py` — auto-selects C or C++ parser by file extension.

### Changed
- `callback_without_gil` check now excludes functions assigned to CPython type slots (tp_dealloc, tp_traverse, etc.), eliminating ~50% of GIL false positives.
- `scan_null_checks.py`: NULL check detection now recognizes Cython-generated patterns (`unlikely(!var)`, `unlikely(var == NULL)`, `__PYX_ERR`).
- `discover_c_files()` now finds C++ source files (.cpp, .cxx, .cc) when tree-sitter-cpp is installed.
- `discover_extension.py` `_find_c_files()` now always finds C++ source files.
- All 9 scanner scripts now use `parse_bytes_for_file()` for language-aware parsing.

## [0.1.1] - 2026-03-21

### Added
- Initial implementation of cext-review-toolkit plugin.
- 10 analysis scripts: tree_sitter_utils, discover_extension, scan_refcounts, scan_error_paths, scan_null_checks, scan_gil_usage, scan_module_state, scan_type_slots, measure_c_complexity, analyze_history.
- 10 agent definitions: refcount-auditor, error-path-analyzer, null-safety-scanner, gil-discipline-checker, module-state-checker, type-slot-checker, stable-abi-checker, version-compat-scanner, git-history-analyzer, c-complexity-analyzer.
- 4 command definitions: explore, health, hotspots, migrate.
- 4 data files: api_tables.json, deprecated_apis.json, stable_abi.json, limited_api_headers.json.
- Tree-sitter-based C parsing for accurate analysis of any extension code style.
- Borrowed-ref-across-callback detection -- the crown jewel finding that regex-based tools cannot achieve.
- Extension discovery supporting setup.py, pyproject.toml, meson.build, CMakeLists.txt, and #include fallback.
- `migrate` command for extension modernization checklists (multi-phase init, stable ABI, version compat).
- Shared script utilities module (`scan_common.py`) for project root detection, file discovery, API table loading.
- Test infrastructure with TempExtension helper, 4 C code fixtures, 1 setup.py template, and 80+ tests.

### Enhanced
- `scan_null_checks.py`: added `deref_macro_on_unchecked` finding type — detects dereference-like macros (PyBytes_AS_STRING, PyList_GET_ITEM, etc.) called on unchecked NULL-able values.
- `scan_type_slots.py`: added `dealloc_missing_xdecref` finding type — detects PyObject* struct members not cleaned up in tp_dealloc.
- `scan_common.py`: `find_assigned_variable()` now skips past ALL_CAPS macro wrappers (e.g., `STATS(x = PyDict_New())`).
- New `scan_version_compat.py` script: detects removed/deprecated API usage, missing version guards, and dead compatibility code.
- `deprecated_apis.json`: added `removed_in` and `version_added` fields, plus entries for PyObject_CallObject, PyEval_CallObject, PyEval_CallObjectWithKeywords.
- `version-compat-scanner` agent now uses `scan_version_compat.py` for script-assisted triage.

### Fixed
- `_check_return_without_exception` false negative: now only suppresses finding when error return is inside a NULL-check block for an exception-setting API.
- `_check_exception_clobbering` false positive: no longer flags `PyErr_SetString` and other exception-setting APIs as clobbering.
- `_check_borrowed_ref_across_call` now detects non-call usage (member access, dereference, assignment) of borrowed refs after intervening Python calls.
- Heap type DECREF check now matches specific DECREF patterns instead of any `Py_TYPE(self)` mention.
- All scanner `main()` functions now use shared `parse_common_args()`.
- Added `callback_without_gil` detection to `scan_gil_usage.py`.

### Documentation
- Added `CLAUDE.md` with project overview, architecture, dev commands, gotchas, and contribution guides.
- Fixed CLAUDE.md: removed incorrect `match` statement claim, added venv/lint commands, added gotchas section.
