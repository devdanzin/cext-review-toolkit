# mypyc Codebase Exploration Report

## Project: mypyc (Python-to-C extension compiler)
## Scope: `mypyc/` subdirectory of mypy repository
## Agents Run: architecture-mapper, git-history-context, complexity-simplifier, silent-failure-hunter, pattern-consistency-checker, tech-debt-inventory

## Executive Summary

mypyc is a well-architected compiler with a clean pipeline (AST → IR → transforms → C codegen) and strong module isolation. However, the C code generation layer (`codegen/`) and C runtime (`lib-rt/`) contain **systematic correctness bugs** that propagate to every mypyc-compiled extension. We traced the 3 bugs found in charset-normalizer's generated output to their exact source locations and discovered **3 additional bugs** in the same code. The C runtime carries significant tech debt (~35 explicit markers) centered on deep coupling to CPython internals via a `#define Py_BUILD_CORE` hack.

## Key Metrics
- **Change Velocity**: Low/maintenance — 9 commits in 18 days, fix:feature ratio 0.75
- **Architecture**: Healthy — clear pipeline, good layer isolation, TYPE_CHECKING cycles handled correctly
- **Complexity**: 3 critical hotspots in `emit.py` (`emit_cast` CC=71, `emit_unbox` CC=55, `emit_box` CC=28) — all type-dispatch functions with per-type branches
- **Tech Debt**: ~35 explicit markers, 294 private `_Py*` API uses in runtime, 6 CPython internal headers via `Py_BUILD_CORE`

## Findings by Priority

### Must Fix (FIX)

1. **[silent-failure-hunter, pattern-consistency, complexity]**: `emit_box` generates no NULL check for `PyFloat_FromDouble`, `PyLong_FromLong`, `PyLong_FromLongLong` — segfault on OOM instead of clean abort
   - `codegen/emit.py:1193-1198`
   - Fix: Add `CPyError_OutOfMemory()` check (3 lines) or wrap in CPy helper functions (~20 lines C)

2. **[silent-failure-hunter, pattern-consistency]**: `generate_dealloc_for_class` missing `Py_DECREF(Py_TYPE(self))` for heap types — type refcount leaks on every instance destroy
   - `codegen/emitclass.py:943`
   - Fix: Save type pointer before `tp_free`, DECREF after (3 lines)

3. **[silent-failure-hunter, pattern-consistency]**: `generate_new_for_class` leaks `self` when `__init__` fails + leaks `Py_None` on success
   - `codegen/emitclass.py:836-840`
   - Fix: `Py_DECREF(self)` on error, `Py_DECREF(ret)` on success (2 lines each)

4. **[silent-failure-hunter]**: `generate_init_for_class` leaks `__init__` return value via ternary — same `Py_None` leak through a different code path
   - `codegen/emitclass.py:787-792`
   - Fix: Store result, check, DECREF on success (3 lines)

5. **[silent-failure-hunter]**: `generate_property_setter` ignores native setter return value — exceptions silently swallowed
   - `codegen/emitclass.py:1235-1255`
   - Fix: Check return, return -1 on error

6. **[tech-debt]**: 5 `PyUnicode_READY` calls will cause **hard compilation failure** on Python 3.15
   - `lib-rt/str_ops.c:99,162,283,330,505`
   - Fix: Remove or version-guard

### Should Consider (CONSIDER)

7. **[tech-debt]**: `#define Py_BUILD_CORE` to access 6 CPython internal headers — fragile, breaks on CPython reorganizations
   - `lib-rt/pythonsupport.h:18-19`

8. **[tech-debt]**: `_PyUnicode_LENGTH` used as lvalue to mutate unicode struct internals — silent data corruption risk
   - `lib-rt/misc_ops.c:517-518,555`

9. **[tech-debt]**: `typealiasobject` struct copied from CPython with "IMPORTANT: must be kept in sync" — no compile-time size check
   - `lib-rt/misc_ops.c:1105-1117`

10. **[silent-failure-hunter]**: `CPyType_FromTemplate` double-allocates `tp_doc`, leaking the first allocation and mutating the static template
    - `lib-rt/misc_ops.c:227-236`

11. **[tech-debt]**: `CPyType_FromTemplate` self-described as "super hacky" with XXX noting `tp_base` first-element assumption "is wrong I think"
    - `lib-rt/misc_ops.c:182,253`

12. **[complexity]**: `emit_unbox` has a HACK comment: "The error handling for unboxing tuples is busted and instead of fixing it I am just wrapping it in the cast code"
    - `codegen/emit.py:1067-1069`

13. **[pattern-consistency]**: `Box` IR op declares `error_kind = ERR_NEVER` but 5 of 7 boxing paths can actually return NULL
    - `ir/ops.py:1149`

### Policy Decisions (POLICY)

14. **[architecture]**: `emitmodule.py` orchestrates the entire pipeline from `codegen/` — should orchestration move to `build.py` or a `pipeline.py`?

15. **[architecture]**: `ll_builder.py` (3066 lines) lives in `irbuild/` but is used by `transform/` and `lower/` — should it be in `ir/`?

16. **[pattern-consistency]**: Should `Box.error_kind` become type-dependent to allow graceful OOM handling instead of abort?

17. **[tech-debt]**: `CPyType_FromTemplate` vs `PyType_FromSpec` — should mypyc migrate to the standard API?

### Acceptable / No Action
- 6 items classified as acceptable across all agents (circular deps via TYPE_CHECKING, type-specific lowering differences, class generation branching)

## Strengths

1. **Clean pipeline architecture** — AST → IR → transforms → C codegen with well-defined interfaces
2. **Excellent IR isolation** — `ir/` package has minimal dependencies, TYPE_CHECKING cycles correctly broken
3. **mypy dependency contained** — only `irbuild/` imports from `mypy.*`; downstream packages are mypy-free
4. **Independent transform passes** — each pass is self-contained with single entry point
5. **Thorough test coverage** — 5776+ lines of class tests, 1380+ lines of string tests added recently
6. **Version-guarded compatibility** — properly uses `CPY_3_XX_FEATURES` guards for version-specific code
7. **`pythoncapi_compat.h` already adopted** — forward compatibility infrastructure in place

## Root Cause Analysis

The 6 code-gen bugs share a common root cause: **mypyc's code generation uses per-type if/elif dispatch chains where each branch independently implements safety-critical operations** (NULL checks, reference counting, cleanup). When a new type or operation is added, the developer must remember to include all safety measures in the new branch. The three monster functions (`emit_cast` CC=71, `emit_unbox` CC=55, `emit_box` CC=28) are where this pattern concentrates. The bugs exist because:

- Float/int boxing was added as simpler single-line emissions, missing the NULL check that tuple boxing has
- The heap type `Py_DECREF(tp)` requirement (CPython 3.8+) was not reflected in the dealloc generator
- `__init__` return value handling was implemented correctly in the native constructor but not in `tp_new`/`tp_init`

## Recommended Action Plan

### Immediate (this week)
1. Fix `emit_box` NULL checks for float/int — 3 lines in `emit.py:1194-1198`
2. Fix `generate_dealloc_for_class` type DECREF — 3 lines in `emitclass.py:943`
3. Fix `generate_new_for_class` self/ret leaks — 4 lines in `emitclass.py:836-840`
4. Fix `generate_init_for_class` ret leak — 3 lines in `emitclass.py:787-792`
5. Fix `generate_property_setter` error swallowing — 3 lines in `emitclass.py:1235-1255`
6. Remove/guard `PyUnicode_READY` calls — 5 sites in `str_ops.c`

### Short-term (this month)
7. Create CPy boxing helper functions (`CPyFloat_BoxDouble`, `CPyLong_BoxLong`, `CPyLong_BoxLongLong`) to centralize OOM handling
8. Add `static_assert(sizeof(typealiasobject))` for copied CPython structs
9. Fix `CPyType_FromTemplate` double `tp_doc` allocation
10. Migrate `PyErr_Fetch`/`PyErr_Restore` to single-exception API (10 call sites)

### Ongoing
11. Evaluate extracting common patterns from `emit_cast`/`emit_unbox`/`emit_box` to prevent future per-branch omissions
12. Track `CPyType_FromTemplate` → `PyType_FromSpec` migration
13. Monitor CPython internal API changes that could break `Py_BUILD_CORE` usage



# mypyc C Runtime (`lib-rt`) Analysis Report — Addendum

## Summary

The 4 agents found **22 confirmed FIX findings** across the runtime — bugs that affect every mypyc-compiled extension. The most critical is a **use-after-free in `CPyDict_NextItem`** and a **triple reference leak in `CPyTagged_TrueDivide`** (called on every big-integer division).

The runtime's dict, list, and singledispatch helpers have systematic patterns of missing NULL checks, leaked references on error paths, and missing `Py_DECREF` on success paths. These are distinct from — and in addition to — the code-gen bugs we found in the Python `codegen/` layer.

---

## Critical Findings (FIX)

### Memory safety

| # | File | Bug | Severity |
|---|------|-----|----------|
| 1 | `dict_ops.c:414-423` | **Use-after-free**: borrowed tuple items DECREF'd then INCREF'd in `CPyDict_NextItem` | CRITICAL |
| 2 | `int_ops.c:33-60` | NULL `PyLong_From*` creates invalid tagged pointer (value 1) — later deref crashes | HIGH |
| 3 | `misc_ops.c:836-837` | `Py_DECREF(NULL)` on `package_path`/`errmsg` in `CPyImport_ImportFrom` | HIGH |

### Reference leaks

| # | File | Bug | Frequency |
|---|------|-----|-----------|
| 4 | `int_ops.c:589-596` | **Triple leak** (`xo`, `yo`, `result`) in `CPyTagged_TrueDivide` slow path | Every big-int division |
| 5 | `dict_ops.c:220-267` | Leaked `list` in `CPyDict_Keys/Values/Items` (×3 functions) | Every `.keys()`/`.values()`/`.items()` on dict subclass |
| 6 | `dict_ops.c:273-278` | Leaked `res` in `CPyDict_Clear` non-exact path | Every `.clear()` on dict subclass |
| 7 | `list_ops.c:36-41` | Leaked `res` in `CPyList_Clear` non-exact path | Every `list.clear()` on list subclass |
| 8 | `dict_ops.c:96-110` | Leaked `new_obj` in `CPyDict_SetDefaultWithEmptyDatatype` on error | Every failed `setdefault` |
| 9 | `str_ops.c:130-132` | Leaked `index_obj` in `CPyStr_GetItem` fallback | Every string index on non-ready unicode |
| 10 | `generic_ops.c:50-54` | Leaked `start_obj` in `CPyObject_GetSlice` | When end alloc fails |
| 11 | `misc_ops.c:898-971` | **Multiple leaks** in `CPySingledispatch_RegisterFunction` — `registry`, `dispatch_cache`, `annotations`, `typing`, `get_type_hints` all leaked on success | Every singledispatch registration |

### Crash bugs (NULL dereference)

| # | File | Bug |
|---|------|-----|
| 12 | `dict_ops.c:220-230` | Unchecked `PyList_New(0)` → `PyList_Extend(NULL, ...)` crash (×3) |
| 13 | `misc_ops.c:935-940` | Two unchecked NULLs in singledispatch `get_type_hints` chain |
| 14 | `dict_ops.c:93` | `PyErr_Clear()` unconditionally swallows non-KeyError exceptions |

### Type slot bugs (runtime's own types)

| # | File | Bug |
|---|------|-----|
| 15 | `function_wrapper.c:22-29` | `CPyFunction_dealloc` missing `Py_DECREF(Py_TYPE(self))` — heap type ref leak |
| 16 | `vecs/librt_vecs.c:135-142` | `VecGenericAlias_dealloc` missing `PyObject_GC_UnTrack` — half-destroyed object visible to GC |

### Buffer management

| # | File | Bug |
|---|------|-----|
| 17 | `internal/librt_internal.c:306` | Realloc-over-self loses original buffer on OOM |

---

## How This Compares to the Code-Gen Bugs

| Category | Code-gen bugs (Python `codegen/`) | Runtime bugs (C `lib-rt/`) |
|----------|-----------------------------------|---------------------------|
| **Count** | 6 systematic patterns, ~40 instances | 17 distinct bugs |
| **Root cause** | Per-type branch dispatch without centralized safety | Missing cleanup on error/success paths |
| **Impact** | Every mypyc extension's generated C | Every mypyc extension's runtime calls |
| **Fixable by** | Patching mypyc's Python code emitter | Patching the C runtime directly |
| **Most critical** | Missing `Py_DECREF(Py_TYPE(self))` in all deallocs | Use-after-free in `CPyDict_NextItem` |

The code-gen bugs are **more widespread** (they repeat for every compiled class), but the runtime bugs include a **use-after-free** (Finding 1) which is a more severe bug class. Together, they paint a picture of a runtime that was developed incrementally with careful handling of the most common paths but incomplete coverage of error paths, non-exact-type paths, and reference lifecycle management.

---

## Priority Fix Order

1. **`CPyDict_NextItem` use-after-free** — INCREF items before DECREF'ing the tuple
2. **`CPyTagged_TrueDivide` triple leak** — add 3 DECREF calls
3. **`CPyImport_ImportFrom` `Py_DECREF(NULL)`** — change to `Py_XDECREF`
4. **`CPyTagged_FromSsize_t/FromVoidPtr/FromInt64`** — add `CPyError_OutOfMemory()` on NULL
5. **`CPyDict_Keys/Values/Items`** — add NULL check + cleanup (3 functions)
6. **`CPySingledispatch_RegisterFunction`** — comprehensive cleanup of all paths
7. **`CPyFunction_dealloc`** — add `Py_DECREF(tp)` for heap type
8. **`VecGenericAlias_dealloc`** — add `PyObject_GC_UnTrack`
9. **Remaining dict/list clear, setdefault, str_ops leaks**
