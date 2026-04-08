# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Safety annotation suppression**: `is_suppressed_by_comment()` convenience function in `scan_common.py` and wired into `scan_refcounts.py`, `scan_null_checks.py`, `scan_error_paths.py`. Findings near `cext-safe:`, `nolint`, `intentional`, etc. comments are tagged `suppressed: true`. (#40)
- **New tests**: `tests/test_scan_common.py` for `deduplicate_findings`, `has_safety_annotation`, `parse_common_args`. Unit tests for `_count_pyarg_format_args()` format parser (15+ branch coverage). (#40)

### Fixed
- `scan_format_strings.py` `main()` now has the standard try/except error envelope matching all other scanners. (#40)
- `scan_resource_lifecycle.py` `_load_resource_pairs()` now exits with error on data file failure instead of silently returning empty dict. (#40)
- `analyze_history.py` `Popen` deadlock: added `proc.terminate()` before `proc.wait()`. (#40)
- `analyze_history.py` unguarded `relative_to()` at line 255 now wrapped in try/except ValueError. (#40)
- `analyze_history.py` `parse_args()` `int()` conversions now handle ValueError with descriptive JSON error messages. (#40)
- Removed 11 unused imports across 6 scripts and 3 unused local variables. (#40)

### Enhanced
- Extracted `_is_guarded_by_exception_setting_api()` from `_check_return_without_exception` in `scan_error_paths.py` (complexity 9/10 → ~4/10). (#40)
- Extracted `_count_c_params()` and `_resolve_slots()` from `scan_type_slots.py` hotspots (complexity 8/10 → ~4/10). (#40)

## [0.2.0] - 2026-04-08

### Added
- **New scanner: `scan_format_strings.py`** — validates format string argument counts for `PyArg_ParseTuple`, `Py_BuildValue`, `PyErr_Format`, `PyUnicode_FromFormat`. Parses both PyArg format codes and printf-style format codes. (#27)
- **New finding type: `stolen_ref_double_free`** in `scan_refcounts.py` — detects `Py_DECREF` on error path after `PyList_SetItem`/`PyTuple_SetItem` (which always steal, even on failure). Found 62 instances across 9 extensions in prevalence scan. Da Woods (Cython) identified this bug class. (#36)
- **New finding type: `method_signature_mismatch`** in `scan_type_slots.py` — validates `PyMethodDef` function signatures match `METH_*` flags (METH_NOARGS→2 params, METH_O→2, METH_VARARGS→2, METH_KEYWORDS→3, METH_FASTCALL→3/4). (#29)
- **New finding type: `object_invalidation_across_gil_release`** in `scan_gil_usage.py` — detects `self->member` use after `Py_END_ALLOW_THREADS` when the same member was accessed before GIL release. Roger Binns (APSW) identified this bug class. (#32)
- **Finding deduplication**: `deduplicate_findings()` utility in `scan_common.py` — groups findings by (type, file, normalized detail), keeps first as canonical with `duplicate_count` field. (#28)
- **Nearby comment checking**: `extract_nearby_comments()` and `has_safety_annotation()` in `scan_common.py` — recognizes `SAFETY:`, `cext-safe:`, `NOLINT`, `intentional`, `by design` annotations near flagged code to reduce false positives. (#30)
- **Code removal opportunities**: new `code_removal_opportunities` section in `deprecated_apis.json` with `PyModule_AddType` (3.10+), `PyImport_ImportModuleAttrString` (3.14+, Roger Binns suggestion), `PyDict_GetItemRef` (3.13+), `PyObject_HasAttrStringWithError` (3.13+). Version-compat agent updated to suggest code removal, not just replacement. (#34)
- **Multi-run support**: `explore` command now accepts `--runs N` and `--informed-reruns` options for the 2-naive + 1-informed methodology that found 33% more bugs on simplejson. (#31)
- **Global finding numbering**: `explore` command synthesis template now numbers all findings sequentially (FIX 1-N, CONSIDER N+1-M, POLICY M+1-P) with action plan referencing by global number. Roger Binns (APSW) requested this. (#33)

### Fixed
- `deprecated_apis.json`: Fixed `PyUnicode_READY` (not actually removed — still exists as no-op in 3.14+), `PyEval_InitThreads` (Py_DEPRECATED marker is 3.9 not 3.7), `PyModule_AddObject` (soft deprecation only, no header marker). `PyObject_CallObject` was already fixed earlier (not deprecated, stable ABI). (#37)
- `test_scan_version_compat.py`: Updated test fixture to use `PyCFunction_Call` (actually removed in 3.13) instead of `PyObject_CallObject`.
- `scan_type_slots.py`: Types inheriting from built-in types (e.g., `PyTuple_Type`) no longer flagged for missing `tp_dealloc` — the base type provides it via inheritance. Based on guppy3 maintainer feedback.
- `scan_refcounts.py`: Borrowed refs from immutable containers (`PyTuple_GetItem`, `PyTuple_GET_ITEM`) no longer flagged as `borrowed_ref_across_call` — tuples hold strong refs and can't be mutated. Based on guppy3 maintainer feedback.
- `scan_null_checks.py`: Added null-safe API set (`PyObject_InitVar`, `Py_XDECREF`, `PyMem_Free`, etc.) — calls to these APIs are no longer flagged as unchecked dereferences. Based on guppy3 maintainer feedback.

### Enhanced
- `refcount-auditor` agent: Added guideline on immutable container borrowed-ref safety.
- `error-path-analyzer` agent: Added guidelines for sentinel/vtable error propagation and defensive visitor/callback patterns.
- `pyerr-clear-auditor` agent: Added guideline for intentional fallback patterns (optional import + fallback).
- `version-compat-scanner` agent: Added guideline to verify deprecation claims against documentation. Added guideline 9: suggest code removal, not just replacement.

### Documentation
- `docs/reproducer-techniques.md`: Added Technique 5b (file-like objects with malicious methods), Technique 20 (str subclass in `sys.modules` for `PyDict_GetItem` error injection), and Technique 21 (mischievous file-like objects for I/O code). Confirmed on msgspec, astropy, and awkward-cpp.

## [0.1.5] - 2026-03-29

### Enhanced
- `type-slot-checker` agent and `scan_type_slots.py`: added `new_and_init_partial_state` triage check. Flags types that define both `tp_new` and `tp_init`, which creates a partial-initialization window between `__new__` and `__init__`. Low confidence, used as a prioritization signal for deeper review of init safety issues. Skips types where `tp_new` is `PyType_GenericNew` (not a custom implementation). Based on APSW maintainer feedback.
- `type-slot-checker` agent: added triage principle to tp_new/tp_init review section — types with no `tp_init` are inherently safe from re-init and partial-init issues.

## [0.1.4] - 2026-03-29

### Enhanced
- `parity-checker` agent: added Python wrapper `__new__`-without-`__init__` safety check. Detects Python classes that wrap C extension types and break when `__new__` is called without `__init__` — methods crash with `AttributeError` on attributes only set in `__init__`. Includes guard pattern recognition (hasattr, getattr with default, try/except, class-level defaults, `__slots__`, attrs set in `__new__`).

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
