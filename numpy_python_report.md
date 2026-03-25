# NumPy Python Codebase Exploration Report

## Project: NumPy
**Scope:** ~1,052 Python files, 275 `.pyi` stubs, 23 Cython files — entire repository
**Agents Run:** 14 (architecture-mapper, git-history-context, consistency-auditor, pattern-consistency-checker, complexity-simplifier, silent-failure-hunter, dead-code-finder, test-coverage-analyzer, documentation-auditor, project-docs-auditor, type-design-analyzer, api-surface-reviewer, tech-debt-inventory, git-history-analyzer)
**Companion report:** `numpy_report.md` covers C extension findings (30 FIX-level bugs in C code). This report covers Python code only — findings already present in the C extension report are excluded.

---

## Executive Summary

NumPy's Python codebase is remarkably well-maintained for its age (~20 years) and scale. Architecture follows a clean layered design centered on the `_core` C kernel. The project is in maintenance/stabilization mode (12:1 fix:feature ratio, 137 commits in 90 days). The type annotation system (275 `.pyi` stubs, 51K lines) is world-class. Core public APIs have 95-100% docstring coverage and consistent conventions.

The most significant Python-side concerns concentrate in two subsystems: **`numpy.ma`** (masked arrays — silent data corruption via `except Exception: pass`, no `__array_function__` dispatch, 8,994-line monolith) and **`numpy.f2py`** (Fortran binding tool — extreme complexity in `crackfortran.py`, 5,769 lines of untested code, dead distutils paths). A third cross-cutting issue is overly broad exception handling in comparison functions (`array_equal`, `array_equiv`) and polynomial operators, which silently hide `MemoryError` and `SystemError`.

---

## Key Metrics

| Dimension | Rating | Summary |
|-----------|--------|---------|
| Architecture | Healthy | Clean layering, managed circular deps, clear public/private split |
| Consistency | Healthy | Strong conventions across core; `ma` and `f2py` are outliers |
| Python Complexity | 10 critical hotspots | 7 in f2py/crackfortran.py, 1 in genfromtxt, 1 in scaninputline, 1 in assert_array_compare |
| Test Coverage | ~79% structural | f2py internals (5,769 lines) and `_core/_internal.py` (962 lines) are critical gaps |
| Error Handling | 10 critical, 10 high | Silent data corruption in `ma/core.py`; broad catches in `array_equal`, polynomial |
| Documentation | 67% coverage | 95-100% for public APIs; user-visible TODO in `help(np.ndarray)`, stale 1.x notes |
| Project Docs | 3 broken, 4 contradictions | Stale Python version in building_with_meson.md, meson-python version mismatch |
| Type System | 9/10 | 275 stubs, zero `Optional` usage, 390 `Incomplete` items (mostly `ma/core.pyi`) |
| Dead Code | ~25 functions | Concentrated in f2py/auxfuncs.py (10), ma/ (3), lib/ (3), _core/ (2), polynomial (2) |
| Tech Debt | 383 markers | f2py distutils dead code ready for removal, 20 empty test stubs, 2 broken tests |
| API Surface | 8/10 learnability | ~400 re-exported names, strong reduction/creation patterns, minor gaps in stacking functions |
| Change Velocity | Active maintenance | 137 commits/90 days, 12:1 fix:feature ratio |

---

## Architecture Summary

NumPy follows a **layered monolithic architecture**:

```
Layer 0: _utils, exceptions, _globals, version         (zero numpy deps)
Layer 1: _core  (119 py + 473 C/H/CPP files)           (THE KERNEL)
Layer 2: lib (58 py), testing, _typing                  (utilities on _core)
Layer 3: linalg, fft, random, ma, polynomial, matrixlib (domain modules)
Layer 4: f2py, ctypeslib, typing                        (tools)
Top:     numpy/__init__.py                              (re-exports ~400 names)
```

**High fan-in:** `_core` (19 dependents), `testing` (14), `_utils` (11), `exceptions` (10)
**Circular deps:** All managed via deferred imports or lazy `__getattr__`. No runtime import failures.
**Key concern:** `lib._shape_base_impl` imports `matrix` from `matrixlib` at module level (acknowledged debt — `np.matrix` is deprecated).

---

## Confirmed FIX Findings (14)

### Silent Data Corruption in Masked Arrays (3)

**F1.** `ma/core.py:1095-1098` — `_MaskedBinaryOperation` silently swallows `copyto` failure with `except Exception: pass`. When restoring original data at masked positions fails, the result array contains uninitialized/garbage values at masked positions. Users calling `.filled()` or accessing `.data` see wrong values with no indication.

**F2.** `ma/core.py:1235-1238` — `_DomainedBinaryOperation` — identical pattern. An entire sequence of operations (copyto, multiply, add) that corrects masked positions is silently discarded on any exception.

**F3.** `ma/core.py:3128-3131` — `__array_finalize__` catches `ValueError` and silently drops the mask (`self._mask = nomask`), exposing previously masked invalid data as if valid. Data that was masked (e.g., division-by-zero results) silently becomes unmasked.

*Note: `ma/core.py:1018-1021` already uses the safer `except TypeError` pattern. F1 and F2 should match.*

### Overly Broad Exception Handling (3)

**F4.** `_core/numeric.py:2529-2532` — `array_equal` catches `Exception` and returns `False`, hiding `MemoryError`, `SystemError`, and bugs in `__array__` methods. Fix: catch `(TypeError, ValueError)` only.

**F5.** `_core/numeric.py:2597-2604` — `array_equiv` — same pattern twice (asarray + broadcast), same fix.

**F6.** `polynomial/_polybase.py:530-612` — All 8 arithmetic operators (`__add__`, `__sub__`, `__mul__`, `__divmod__`, `__radd__`, `__rsub__`, `__rmul__`, `__rdivmod__`) catch `Exception` and return `NotImplemented`, hiding memory errors and internal bugs. Fix: catch `(TypeError, ValueError)` only.

### Tech Debt Ready for Removal (3)

**F7.** `f2py/f2py2e.py:581-655` — Distutils dead code. The TODO at line 650 says "once min_ver >= 3.12, unify into --fflags." Min Python is now 3.12. The `--backend distutils` option is accepted but immediately overridden to meson. `reg_distutils_flags` regex and `distutils_flags` filtering are dead code.

**F8.** `numpy/__init__.py:905` — `NPY_PROMOTION_STATE` environment variable check. TODO explicitly says remove. The deprecation warning says it was ignored after NumPy 2.2.

**F9.** `lib/tests/test_recfunctions.py:556, 824` — Tests marked `FIXME: broken` with assertions commented out. These silently pass, giving false confidence in test coverage.

### Documentation Errors Affecting Users (3)

**F10.** `doc/TESTS.rst:42` — Says `numpy.test(label='slow')` runs "NumPy's full test suite." It actually runs only slow-marked tests. Correct is `label='full'` (documented correctly at line 221 in the same file).

**F11.** `_core/_add_newdocs.py:2426` — User-visible TODO in `help(np.ndarray)`: `"; TODO)"` in the Attributes section. Visible to every user who reads ndarray help.

**F12.** `exceptions.py:43` — Reload error message says `'Reloading numpy._globals is not allowed'` but this code is in `numpy.exceptions`.

### Cross-File Contradictions (2)

**F13.** `requirements/build_requirements.txt:1` — `meson-python>=0.13.1` contradicts `pyproject.toml:4` which requires `meson-python>=0.18.0`. Developers installing from requirements get an older version that doesn't meet the actual build requirement.

**F14.** `building_with_meson.md:4` — Claims "Python 3.10-3.12" support and references `python3.10` in PYTHONPATH examples. Actual minimum is Python 3.12 (`pyproject.toml:16`). The "early adopters" framing is also stale — Meson is the default and only build system.

---

## Incomplete Fix Propagation (from git-history-analyzer)

### Fix Not Propagated to Analogous Code (1)

**P1.** Commit `41f3673` fixed infinite recursion in `flatten_structured_array` by adding `not isinstance(elm, (str, bytes))` to `flatten_sequence` at `ma/core.py:2593`. Two structurally similar functions were NOT updated:
- `_flatsequence` at `ma/core.py:1857-1866` — has `hasattr(element, '__iter__')` without str/bytes guard
- `flatten_inplace` at `ma/extras.py:340-347` — has `hasattr(seq[k], '__iter__')` without the guard (this becomes an infinite loop on string elements, not just recursion)
- `_izip_fields` at `lib/recfunctions.py:293` — excludes `str` but NOT `bytes`

---

## CONSIDER Findings (19)

### Python Complexity Hotspots (5)

**C1.** `f2py/crackfortran.py` — 7 critical hotspots. `analyzevars` (329 code lines, cognitive=497, 61 local vars, score 10/10) and `analyzeline` (526 code lines, cognitive=477, 74 local vars, score 10/10) are the two most complex functions in the entire Python codebase. Root cause: global state parser operating at multiple abstraction levels. Systemic fix: encapsulate parser state into `FortranParserState` class.

**C2.** `f2py/f2py2e.py:195-335` `scaninputline` — score 10/10. Uses cryptic flag variables (`f`, `f2`, `f3`, `f5`, `f6`, `f8`, `f9`, `f10`) as state machines for argument parsing. Immediate fix: rename to descriptive names (`expect_signsfile`, `expect_modulename`, etc.). Longer-term: migrate to argparse.

**C3.** `lib/_npyio_impl.py:1735-2488` `genfromtxt` — 425 lines, 25 params, 73 local vars, score 10/10. Extract: `_process_missing_values`, `_process_filling_values`, `_initialize_converters`, `_construct_output_array`.

**C4.** `_core/einsumfunc.py:445-621` `_parse_einsum_input` — score 9/10. Duplicated subscript conversion logic at lines 499-511/516-527 and 582-592/598-606. Extract `_subscript_list_to_string` and `_build_output_subscripts`.

**C5.** `testing/_private/utils.py:734-992` `assert_array_compare` — score 10/10. Extract error reporting (lines 904-984) into `_build_mismatch_report`.

### Silent Failure Patterns (3)

**C6.** `f2py/capi_maps.py:159` — `eval()` on user-provided `.f2py_f2cmap` file content with `except Exception`. Replace with `ast.literal_eval()`.

**C7.** `_core/numerictypes.py:221-224` `obj2sctype` — catches `Exception` and returns default (`None`), hiding `MemoryError`/`SystemError`. Fix: catch `(TypeError, ValueError)`.

**C8.** `f2py/crackfortran.py` — ~15 `except Exception: pass` blocks throughout the Fortran parser. Add debug-level logging rather than silent `pass`.

### Test Coverage Gaps (3)

**C9.** f2py internals: 5,769 lines with zero unit tests across `auxfuncs.py` (1,005 lines), `rules.py` (1,641 lines), `capi_maps.py` (811 lines), `cfuncs.py` (1,579 lines), `cb_rules.py` (649 lines). Only integration tests exist. Priority: `capi_maps.py` type mappings, then `crackfortran.py::analyzeline` statement parsing.

**C10.** `_core/_internal.py` (962 lines) — PEP 3118 buffer protocol parser `_dtype_from_pep3118` nearly untested. Add format string test suite.

**C11.** FFT `out` parameter error paths — Recent bugfix (845f93c) for `hfft`/`ifft2`/`irfft2` added happy-path tests only. Add `test_hfft_bad_out`, `test_ifft2_bad_out`, `test_irfft2_bad_out` mirroring existing `test_fft_bad_out`.

### Dead Code (3)

**C12.** 10 dead functions in `f2py/auxfuncs.py`: `_isstring`, `isunsignedarray`, `issigned_chararray`, `issigned_shortarray`, `issigned_array`, `ismutable`, `hasvariables`, `hasinitvalueasstring`, `istrue`, `isfalse`.

**C13.** `_core/getlimits.py:16,23` — `_fr0`/`_fr1` defined but never called. `_core/_internal.py:390` — `_copy_fields` never called. `polynomial/chebyshev.py:275,307` — `_zseries_der`/`_zseries_int` never called (have `.pyi` stubs that also need cleanup).

**C14.** `lib/_datasource.py:44` `_check_mode`, `lib/_iotools.py:49` `_is_bytes_like`, `lib/_utils_impl.py:123` `_get_indent`, `ma/extras.py:58` `issequence`, `ma/mrecords.py:35,69` `_checknames`/`_get_fieldmask`, `_utils/__init__.py:41` `_rename_parameter`.

### Pattern Divergence (3)

**C15.** `ma/core.py:4531` and `mrecords.py:712` — `warnings.warn()` without category (defaults to `UserWarning`); also redundant `"Warning: "` prefix in messages. Every other module specifies explicit categories (`RuntimeWarning`, `DeprecationWarning`, etc.).

**C16.** f2py has duplicate `outmess` implementations — `auxfuncs.outmess(t)` (line 59) vs `crackfortran.outmess(line, flag=1)` (line 235) — same name, different signatures, different behavior.

**C17.** `lib/_datasource.py:255` `DataSource` and `Repository` rely solely on `__del__` for cleanup with no context manager support. `NpzFile` in the same package implements both `__enter__`/`__exit__` and `__del__` (the modern best practice).

### Documentation (2)

**C18.** Linalg result NamedTuples (`EigResult`, `EighResult`, `QRResult`, `SlogdetResult`, `SVDResult`) and `UniqueAllResult`/`UniqueCountsResult`/`UniqueInverseResult` — returned by major public APIs but have no docstrings.

**C19.** `_core/fromnumeric.py:1682-1703` — `diagonal()` Notes section has 20 lines of NumPy 1.7/1.8/1.9 migration text. Also: `argsort()` and `searchsorted()` have "As of NumPy 1.4.0" notes from 2009.

---

## API Surface Findings (4 — additive, non-breaking)

**A1.** `dstack` and `column_stack` lack `dtype`/`casting` parameters that `hstack`, `vstack`, `concatenate`, and `stack` all have. Completing this pattern is additive.

**A2.** `logspace` and `geomspace` lack `device` parameter that `linspace` has. These three form a tight conceptual group.

**A3.** `cond(x, p=None)` uses `p` while `norm`, `matrix_norm`, `vector_norm` all use `ord`. Three different tolerance names (`rcond`, `tol`, `rtol`) across `pinv` and `matrix_rank` in the same file.

**A4.** `_core/arrayprint.py:31-32` — Dead `_dummy_thread` fallback import (removed in Python 3.9, NumPy requires 3.12+). Replace try/except with direct `from _thread import get_ident`.

---

## Policy Decisions (6)

**POL1.** Decide on `__array_function__` dispatch for `numpy.ma` — currently the only major module without it (~100+ public functions). Likely intentional but undocumented.

**POL2.** Standardize test assertion style — `assert_raises` (2,653 uses) vs `pytest.raises` (850 uses). Recommend `pytest.raises` for new code; do not mass-convert existing.

**POL3.** f2py `crackfortran.py` global state — consider encapsulating into `FortranParserState` class. Dedicated project, not a quick fix.

**POL4.** Document import convention: `__init__.py` uses relative imports; `_impl` modules use absolute imports from `numpy._core`.

**POL5.** Track `Incomplete` count in type stubs (currently 390) as a metric; consider CI regression check. Focus: `ma/core.pyi` (85), `_ufunc.pyi` (35), `__init__.pyi` (48).

**POL6.** Establish tiered docstring coverage targets: 100% public API (currently 95-100%), 50%+ internal modules (currently ~30% for f2py).

---

## Tech Debt Summary (383 markers)

| Category | Count | Top Locations |
|----------|-------|--------------|
| NOQA | 224 | test_generator_mt19937 (35), test_randomstate (25), test_umath (23) |
| TODO | 89 | f2py/symbolic.py (9), f2py/crackfortran.py (4), lib/_datasource.py (4) |
| FIXME | 24 | test_recfunctions (2 broken tests), ma/core.py (1 design question) |
| XXX | 19 | f2py/crackfortran.py (7), test_umath_complex (3) |
| type:ignore | 24 | typing tests (12 intentional), _nbit_base (6 structural) |
| HACK | 3 | _core/_dtype.py (1), ma/core.py (2) |

**20 empty test stubs** in `f2py/tests/test_f2py2e.py` (`# TODO: populate` + `pass` body) — many test distutils-related CLI flags that are now dead code.

**~4,500 lines of deprecated modules**: `core/` (19 shim files), `matrixlib/` (2,380 lines), `_core/defchararray.py` (1,426 lines), `matlib.py` (380 lines), `char/` (31 lines).

---

## Strengths

1. **World-class type annotations**: 275 `.pyi` stubs (51K lines), zero `Optional` usage (all `X | None`), consistent `_co` suffix convention, PEP 695 `type` statements, `@type_check_only` on internal types.

2. **Clean layered architecture**: `_utils` -> `_core` -> `lib` -> domain modules. Circular deps all managed with deferred imports. No runtime failures.

3. **Mature public API**: ~400 re-exported names with consistent parameter conventions (`axis`, `out`, `dtype`, `keepdims`). `@set_module` and `@array_function_dispatch` applied uniformly across core modules. Learnability rated 8/10.

4. **Comprehensive test suite**: 181 test files, 7,030 functions, 122K lines. Strong parametrized tests. Recently added `as_strided_checked` has 13 dedicated tests.

5. **Active maintenance discipline**: 12:1 fix:feature ratio, ruff cleanup across 27 files, active mypy_primer workflow, consistent dependency versioning.

6. **Effective deprecation machinery**: Uniform `__getattr__` + `_raise_warning` shim pattern across all 19 `core/` stub modules.

7. **Lazy loading strategy**: Major submodules loaded via `__getattr__` in `numpy/__init__.py` keeps `import numpy` fast.

---

## Churn x Quality Risk Matrix

| Rank | Module | Churn (90d) | Complexity | Test Coverage | Error Issues | Risk |
|------|--------|-------------|------------|---------------|--------------|------|
| 1 | `f2py/crackfortran.py` | 3 commits | Score 10 (7 hotspots) | Zero unit tests | 15 `except Exception: pass` | **CRITICAL** |
| 2 | `ma/core.py` | 4 commits | 8,994 lines monolith | Adequate | 3 silent data corruption | **HIGH** |
| 3 | `f2py/cfuncs.py` | 6 commits | — | Zero unit tests | — | **HIGH** |
| 4 | `f2py/auxfuncs.py` | 2 commits | — | Zero unit tests | 10 dead functions | **MEDIUM** |
| 5 | `f2py/rules.py` | 2 commits | — | Zero unit tests | — | **MEDIUM** |
| 6 | `f2py/capi_maps.py` | 2 commits | — | Zero unit tests | `eval()` on file input | **MEDIUM** |
| 7 | `_core/numeric.py` | 3 commits | — | Good | `array_equal` broad catch | **LOW** |
| 8 | `lib/_npyio_impl.py` | 1 commit | Score 10 (genfromtxt) | Good | — | **LOW** |
| 9 | `polynomial/_polybase.py` | 1 commit | — | Good | 8 broad `except` | **LOW** |

---

## Recommended Action Plan

### Immediate (this week)

| # | Item | Effort | Finding |
|---|------|--------|---------|
| 1 | Narrow `array_equal`/`array_equiv` to `(TypeError, ValueError)` | 2-line fix | F4, F5 |
| 2 | Remove `NPY_PROMOTION_STATE` check | 5-line removal | F8 |
| 3 | Fix `TESTS.rst:42` label (`slow` -> `full`) | 1-line fix | F10 |
| 4 | Fix `build_requirements.txt` meson-python version | 1-line fix | F13 |
| 5 | Remove user-visible TODO in `help(np.ndarray)` | 1-line fix | F11 |
| 6 | Fix reload error message in `exceptions.py` | 1-line fix | F12 |
| 7 | Remove dead `_dummy_thread` fallback | 3-line fix | A4 |

### Short-term (this month)

| # | Item | Effort | Finding |
|---|------|--------|---------|
| 8 | Add warnings to `ma/core.py` masked restoration (1095, 1235) | Small | F1, F2 |
| 9 | Narrow polynomial operator catches to `(TypeError, ValueError)` | Small (8 sites) | F6 |
| 10 | Remove f2py distutils dead code + 20 empty test stubs | Medium | F7 |
| 11 | Remove 25 dead Python functions across 8 files | Medium | C12-C14 |
| 12 | Propagate str/bytes guard to `_flatsequence` and `flatten_inplace` | Small | P1 |
| 13 | Update `building_with_meson.md` Python versions | Small | F14 |
| 14 | Enable ruff UP031 (auto-convert ~72 %-format strings) | Automated | — |
| 15 | Rename `scaninputline` flag variables | Small | C2 |
| 16 | Fix/remove broken test_recfunctions tests | Small | F9 |
| 17 | Add warning categories to `ma` warnings | Small | C15 |

### Ongoing

| # | Item | Scope | Finding |
|---|------|-------|---------|
| 18 | Add f2py unit tests (start: `capi_maps.py`, then `crackfortran.py`) | Large | C9 |
| 19 | Reduce `Incomplete` in type stubs (focus: `ma/core.pyi`) | Medium | POL5 |
| 20 | Decompose `genfromtxt` into helper functions | Medium | C3 |
| 21 | Add `dtype`/`casting` to `dstack`/`column_stack` | Small | A1 |
| 22 | Add `device` to `logspace`/`geomspace` | Small | A2 |
| 23 | Plan f2py `crackfortran.py` global state refactor | Project-level | POL3 |
| 24 | Docstrings for linalg/unique result NamedTuples | Small | C18 |
| 25 | Condense stale NumPy 1.x version notes in docstrings | Small | C19 |
