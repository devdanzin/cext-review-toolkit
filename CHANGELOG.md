# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Enhanced
- **Q4 (`scan_cython_cinit_candidates.py`) gains Shape B detection** (#53): the v0.4.0 Q4 only caught the cymem-shape (`__cinit__` and `__init__` both assigning the same field). Production-agent verification on blosc2 surfaced this as a script gap — the F3 (`SChunk`) and F4 (`NDArray`) reinit-leaks have a different shape: a `cdef <T>* field` declaration, an `__init__` that allocates into it without a free guard, and a `__dealloc__` that frees it. No `__cinit__` is involved. Q4 now applies a second detection pass that triggers on this pointer-field shape and emits FIX HIGH. Validated: catches `SChunk.__init__` (blosc2_ext.pyx:1523) and `NDArray.__init__` (blosc2_ext.pyx:3415), correctly skips `vlmeta` (no `__dealloc__` → non-owning view) and `slice_flatter` (memoryview fields, not raw pointers); cymem `Address.__init__` (cymem.pyx:185) still caught via Shape A with no double-emit. Findings now carry a `details.shape` discriminator (`"overlap"` for Shape A, `"pointer_field"` for Shape B).

## [0.4.0] - 2026-04-27

### Added
- **Generated-code-mapper agent** (`plugins/cext-review-toolkit/agents/generated-code-mapper.md`): a Phase-0 orientation specialist that runs FIRST in every explore on a code-generator-using extension (Cython, pybind11, nanobind, custom codegen). Produces `reports/<extension>_v1/preflight/generated_code_map.md` containing a file-classification table, ACCEPTABLE-idiom regex catalogue, project-specific structural patterns, and AST-script-finding triage hints. Validated on cymem (126 lines), uvloop (132 lines), and blosc2 (174 lines): all hand-verification hints confirmed via source-reading; novel patterns surfaced (cymem's `Address.__init__` reinit-leak; uvloop's libuv-callback architecture and RAII context-object pattern; blosc2's F11 line-level confirmation).
- **Cython AST scanner suite** (`plugins/cext-review-toolkit/scripts/`):
  - `cython_ast_utils.py` — shared parser + walker + Cython-specific structural helpers built on tree-sitter-cython (devdanzin/tree-sitter-cython@b2f6fd0 fork closes 19 grammar gaps from upstream b0o)
  - `scan_cython_cdef_int_except.py` (Q1) — silent-noexcept on `cdef int` callbacks (the F-cluster blosc2 pattern; 15-site exact match on blosc2 ground truth)
  - `scan_cython_buffer_protocol.py` (Q2) — `PyObject_GetBuffer` not paired with `PyBuffer_Release` in `try/finally` (HIGH/MEDIUM tiers based on whether any release exists)
  - `scan_cython_pycapsule.py` (Q3) — `PyCapsule_New(ptr, name, NULL)` candidates (the F11 multi-wrap-leak pattern)
  - `scan_cython_cinit_candidates.py` (Q4) — `__cinit__`/`__init__` field-reassignment reinit-leak (the cymem `Address.__init__` and blosc2 F3-F8 pattern; HIGH when RHS is a function call)
  - `scan_cython_nogil_pyobject.py` (Q5) — Python-level operations (`raise`, `print`, f-strings, comprehensions) inside `nogil` scopes without `with gil:` re-acquisition
- **Cython playbook** (`plugins/cext-review-toolkit/data/playbooks/cython.md`): 11-section orientation reference covering Cython 3.0/3.1/3.2/3.3+, ACCEPTABLE-idiom table (12 grep regexes), 7 bug-pattern subsections (silent-noexcept, `__cinit__`/`__init__`, buffer protocol, PyCapsule, nogil-touches-GIL, FT status, `__dealloc__`-holds-GIL), project-specific patterns calibrated against blosc2 + uvloop, version-difference matrix, prompt-fragment template for downstream agents.
- **Preflight directive** added to all 13 downstream audit-agent prompts (refcount-auditor, error-path-analyzer, gil-discipline-checker, null-safety-scanner, module-state-checker, type-slot-checker, stable-abi-checker, version-compat-scanner, pyerr-clear-auditor, resource-lifecycle-checker, parity-checker, c-complexity-analyzer, git-history-analyzer): each instructs the agent to read `reports/<extension>_v1/preflight/generated_code_map.md` before Phase 1 if it exists, and apply its file classification, idiom filters, and project-specific patterns to triage findings. Closes the major false-positive vector observed during validation (uvloop's RAII context-object pattern, blosc2's reference-template-driven Q2 triage).

### Notes
- Q4/Q5 require the devdanzin tree-sitter-cython fork: `pip install git+https://github.com/devdanzin/tree-sitter-cython.git@fix/grammar-gaps`. The upstream b0o repo has 19 grammar gaps that block clean parsing of real-world .pyx files; the fork's `fix/grammar-gaps` branch (commit b2f6fd0) closes all of them with zero regressions on a 51-file calibration corpus.

### Carried over from prior session (still in [Unreleased])
- **Reproducer catalog T24 + T25** (carried over from prior session): Technique 24 (RSS growth monitoring for leaks invisible to tracemalloc — confirmed on couchbase-python-client `pycbc_streamed_result` 786 B/cycle and `pycbc_hdr_histogram.__init__` 4.5 KB/re-init); Technique 25 (SystemError probe for PyCFunction contract violations — confirmed on couchbase-python-client `_core` with 8 SystemError observations). (#50)
- **Reproducer catalog T26 — Cyclic-GC threshold coercion** (#50): `gc.set_threshold(1, 1, 1)` forces gen-0 collect on every tracked allocation, turning timing-dependent `tp_traverse` / `tp_clear` races during partial init into deterministic SIGSEGVs on iteration 0 or 1. Confirmed on frozendict 2.4.7 F12 subclass GC UnTrack gap (5/5 deterministic across runs).
- **Reproducer catalog T27 — Subprocess-isolated dense OOM sweep** (#50): launch each target code path in its own subprocess with libfiu `LD_PRELOAD` + `PYTHONMALLOC=malloc` so SIGSEGVs in one path don't kill the driver; classify by exit code (139=SEGV, 134=ABRT, 10=clean MemoryError). Complements T23 (surgical) and T18 (CPython-allocator). Confirmed on frozendict 2.4.7 F7+F8 — 4 construction paths × 30 offsets, all 120 runs SIGSEGV.
- **Reproducer catalog T28 — ctypes struct-field probe via `id + offset`** (#50): read refcounts/counters on internal CPython structs (`Py_EMPTY_KEYS.dk_refcnt`, keys-table fields) that aren't exposed via `sys.getrefcount`. Works by computing `id(obj) + field_offset`, casting through `ctypes.POINTER`, dereferencing. Confirmed on frozendict 2.4.7 F13 `fromkeys` leak — exactly +1.000 `dk_refcnt` per call.
- **Reproducer catalog T29 — MRO unbound-method bypass enumeration** (#50): for pure-Python "frozen"/"readonly" wrappers that inherit from mutable types, programmatically exercise every `Base.method(instance, *args)` unbound call and check for observable state changes. Reveals incomplete immutability overrides. Confirmed on frozendict 2.4.7 pure-Python fallback — 14 distinct mutation-bypass routes (`dict.__setitem__`, `dict.update` × 3 signatures, `dict.clear`, `super().__init__`, and a `dict.__init__` merge-semantic surprise).

## [0.3.0] - 2026-04-18

### Added
- **Cross-toolkit sync — shared triage helpers** (#49, ported from ft-review-toolkit): `make_finding()` factory and `is_in_region()` utility in `scan_common.py`; cext-tuned `_SAFETY_KEYWORDS` (drops `mutex held`, adds `refcount safe`, `borrowed ok`, `gil held`, `gil-held`, `already locked`, `already protected`, `thread-safe`). `make_finding` is shipped unadopted — existing scanner finding-dict construction is unchanged to avoid a breaking output-shape change.
- **`analyze_history.py` parallelization** (#49, ported from cpython-review-toolkit): `ThreadPoolExecutor`-based parallel `git show` in `compute_function_churn` and `get_commit_details`; new `--workers N` CLI flag (default 8, `_parse_int`-validated) and top-level `_fetch_one_diff` helper.
- **Agent prompt scaffolding** (#49): "Running the script" footer added to all 11 script-backed agents (300000 ms Bash timeout, PID-suffixed temp filenames, `--workers` forwarding, no-retry fallback). "Confidence" section (HIGH ≥90% / MEDIUM 70–89% / LOW 50–69%, below-LOW not reported) added to 12 agents. New "Fix Completeness Review" phase (Phase 2) in `git-history-analyzer` ported from cpython, covering goto-cleanup labels, `#ifdef` platform variants, and affected-variable completeness; effort split updated from 70/30 similar-bug/churn to 60/15/25 similar-bug / fix-completeness / churn+contextual.
- **Envelope sanity tests** (#49): `test_scan_gil_usage`, `test_scan_module_state`, `test_scan_type_slots`, and `test_scan_version_compat` now assert `functions_analyzed >= 1` so a silent data-file or parser regression cannot let a scanner ship with empty output.
- **`test_scan_common` extended coverage** (#49): `TestExtractNearbyComments`, `TestIsInRegion`, `TestMakeFinding`, and cext-specific safety-keyword assertions.
- **libfiu reproducer infrastructure** under `docs/`: `docs/libfiu_helpers.py` (reusable context managers `nth_allocation`, `enable_if`, `from_stack_of`, plus `promote_to_global` and `require_preloaded` utilities) and `docs/libfiu_zstd_validation.py` (end-to-end validation reproducer against CPython's stdlib `_zstd` extension and `libzstd.so.1`). Full setup, usage, and 8 documented gotchas in `docs/reproducer-techniques.md` Technique 23, including the `ctypes.CDLL("libc.so.6")` bypass trap and the `RTLD_LOCAL` symbol-visibility issue. Complements Technique 18 (`_testcapi.set_nomemory`) which only reaches Python's own allocator; libfiu reaches system malloc and thus foreign C libraries linked by the extension.
- **Dense-sweep methodology** documented in `docs/reproducer-techniques.md` Technique 18 rewrite: correct sweep harness (subprocess-per-iteration, full `[0..N]` range, not sparse samples), allocator-domain clarification (`set_nomemory` hooks all three PyMem domains, not just PYMEM_DOMAIN_RAW as earlier docs implied), exit-code meanings, and a list of common pitfalls (piped-invocation hides segfaults, in-process loops only catch the first crash, module-init OOM testing needs subprocess isolation). Surfaced [gh-146092](https://github.com/python/cpython/issues/146092) — a CPython OOM NULL-deref in `_PyFrame_GetLocals` (since fixed upstream on 2026-03-18 by commit `e1e4852133e`).
- **No-exception API allowlist** in `data/api_tables.json` (`no_exception_apis` key). Curated list of 69 CPython APIs that cannot set a Python exception under any circumstances, in 6 categories: refcount macros (`Py_INCREF`, `Py_XINCREF`, `Py_REFCNT`, `Py_SET_REFCNT`), type-access macros (`Py_TYPE`, `Py_SIZE`, `Py_IS_TYPE`, etc.), 48 type-check macros (`PyList_Check`, `PyDict_Check`, `PyCFunction_Check`, etc.), exception inspection (`PyErr_Occurred`, `PyErr_ExceptionMatches`, `PyErr_GivenExceptionMatches`), exception state management (`PyErr_Fetch`, `PyErr_Restore`, `PyErr_GetRaisedException`, `PyErr_SetRaisedException`, `PyErr_NormalizeException`), and GC tracking (`PyObject_GC_Track`, `PyObject_GC_UnTrack`, `PyObject_GC_IsTracked`, `PyObject_GC_IsFinalized`). Regression-tested in `tests/test_scan_error_paths.py::test_no_exception_macros_not_flagged_as_clobber` and `test_exception_state_and_gc_apis_not_flagged_as_clobber`. (wrapt v2 re-audit 2026-04-12, msgspec sweep 2026-04-14)
- **Non-erroring int-API allowlist** in `data/api_tables.json` (`non_erroring_int_apis` key). More specific curated list of int-returning CPython APIs whose headers explicitly document "does not raise exceptions" (currently `PyUnicode_CompareWithASCIIString`, `Py_IS_TYPE`, `PyObject_TypeCheck`, `PyIndex_Check`, `PyCallable_Check`, `PyNumber_Check`, plus `PyUnicode_Tailmatch` with a caveat note). Their `-1` return is a comparison result or flag, NOT an error signal. Each entry cites the specific header file and line for future verification. A subset of `no_exception_apis` above, kept separate for detailed agent-level guidance in the error-path-analyzer prompt. Regression-tested in `tests/test_scan_error_paths.py::test_non_erroring_api_not_flagged_as_clobber`. (wrapt v2 re-audit 2026-04-12)
- **Safety annotation suppression**: `is_suppressed_by_comment()` convenience function in `scan_common.py` and wired into `scan_refcounts.py`, `scan_null_checks.py`, `scan_error_paths.py`. Findings near `cext-safe:`, `nolint`, `intentional`, etc. comments are tagged `suppressed: true`. (#40)
- **New tests**: `tests/test_scan_common.py` for `deduplicate_findings`, `has_safety_annotation`, `parse_common_args`. Unit tests for `_count_pyarg_format_args()` format parser (15+ branch coverage). (#40)

### Fixed — false positives identified by the wrapt v2 re-audit (2026-04-12) + follow-up sweep (2026-04-14)
- **`scan_error_paths.py::_check_exception_clobbering`** now filters out calls to documented non-raising APIs via a new `_apis_that_cannot_clobber()` helper that reads both `no_exception_apis` and `non_erroring_int_apis` from the data file. Previously, any `Py*`/`_Py*` call inside an error-handling block was flagged as "could clobber the pending exception" — but APIs that never raise cannot clobber. Measured reduction across 7 extensions (simplejson, bitarray×2, msgspec, pyerfa, psutil, wrapt): **186 → 117 total `exception_clobbering` findings (37.1% reduction)**, with per-extension variance from 13.7% (msgspec, dense complex error handling) to 66.7% (bitarray_util, heavy type-dispatch in error paths). On wrapt specifically: 51 → 30 (41.2%), filtering `Py_INCREF` × 10, `PyErr_ExceptionMatches` × 9, `Py_TYPE` × 1, `PyCFunction_Check` × 1. On msgspec: 102 → 72 (29.4%), filtering `Py_INCREF` × 8, `PyObject_GC_Track` × 8, `Py_TYPE` × 5, `PyErr_Fetch` × 4, `PyErr_Restore` × 4, `PyUnicode_CheckExact` × 1. The exception-state management and GC tracking entries were added in the 2026-04-14 sweep after observing them as the dominant remaining false-positive class in msgspec. This also directly fixes wrapt v2 findings #19 and #33 (`PyUnicode_CompareWithASCIIString(x, "...") == 0` incorrectly classified as "-1/0 conflation"; CPython `Include/unicodeobject.h:957` explicitly states the function does not raise exceptions). Issue #42 tracks a future refactor to an opt-in model (maintain `can_raise_apis` instead of an allowlist of what can't raise) for further noise reduction.
- **`agents/error-path-analyzer.md`** gained Phase-3 guidance #7 ("Int-returning APIs that CANNOT raise exceptions") with the historical false-positive warning, a cross-reference to `data/api_tables.json`, and step-by-step instructions to verify a function's exception behavior by reading its CPython header declaration and body.
- **`agents/parity-checker.md`** gained Phase 3a ("Slot-Regression Claims MUST Use Behavioral Verification") with concrete examples of why Python-level descriptor identity (`T.__setattr__ is Base.__setattr__`) and `in T.__dict__` membership checks are unreliable, and the required live-behavioral-test pattern. Direct fix for wrapt v2 findings #10 and #11 (BFW slot regressions falsified by behavioral test).
- **`agents/type-slot-checker.md`** gained Phase-3 guidance #5 with the same behavioral-verification requirement for claims like "slot declared but not wired" or "regressed to the base class". Cross-references the same wrapt historical false positives.

### Fixed
- **Data-file loader silent failure** (#49): `scan_common.load_api_tables`, `scan_version_compat._load_deprecated_apis`, and `scan_resource_lifecycle._load_resource_pairs` now print `WARNING: Failed to load <path>: <err>` to **stderr** before exiting, and route the JSON error envelope to stderr as well. Previously the api_tables and deprecated_apis loaders printed the JSON error to stdout, polluting downstream JSON parsing.
- **`analyze_history.py` pipe-deadlock hardening** (#49): the streaming `git log` `Popen` is now terminated with `wait(timeout=5)` and a `kill()` + final `wait()` fallback on `TimeoutExpired`, preventing the child from zombifying when `parse_git_log` stops reading early (e.g., when `max_commits` is hit before git finishes writing).
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
