# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

### Fixed
- `_check_return_without_exception` false negative: now only suppresses finding when error return is inside a NULL-check block for an exception-setting API.
- `_check_exception_clobbering` false positive: no longer flags `PyErr_SetString` and other exception-setting APIs as clobbering.
- `_check_borrowed_ref_across_call` now detects non-call usage (member access, dereference, assignment) of borrowed refs after intervening Python calls.
- Heap type DECREF check now matches specific DECREF patterns instead of any `Py_TYPE(self)` mention.
- All scanner `main()` functions now use shared `parse_common_args()`.
- Added `callback_without_gil` detection to `scan_gil_usage.py`.
