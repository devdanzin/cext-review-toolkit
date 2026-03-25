---

# charset-normalizer C Extension Analysis Report (mypyc-generated)

## Executive Summary

charset-normalizer is compiled from Python to C by **mypyc** (the mypy compiler backend), producing a single 21K-line C file with ~400 functions. This is our first analysis of mypyc-generated code, and the results reveal **three systematic mypyc code-generator bugs** that affect all mypyc-compiled extensions, not just charset-normalizer.

The scanner results were dramatically inflated (136 refcount, 402 error-path, 879 external tool findings) because the scanners are tuned for hand-written C patterns, not mypyc's mechanical code generation idioms. After deep triage, **>95% are false positives**. The true findings are highly focused:

1. **`PyFloat_FromDouble` never NULL-checked** — 13 instances where OOM causes segfault instead of clean error. mypyc guards `PyTuple_New` and `CPyTagged_StealAsObject` with `abort()` but omits the guard for float boxing.

2. **Missing `Py_DECREF(Py_TYPE(self))` in all 17 heap type deallocs** — type refcount inflates by 1 per instance destroyed, a monotonic leak.

3. **Reference leak of `self` + `Py_None` in all 10 `tp_new` functions** — when `__init__` fails, `self` is leaked; on success, the `Py_None` return from `__init__` is leaked.

All three are **mypyc compiler bugs** — charset-normalizer maintainers cannot fix them without patching mypyc upstream.

**Total: 3 systematic mypyc bugs (40 individual instances), 0 charset-normalizer-specific bugs.**

---

**Project:** charset-normalizer — Character encoding detection (mypyc-compiled)
**Source:** `~/projects/laruche/repositories/charset-normalizer/build/`
**Stats:** 1 main file (21K lines), 2 init stubs, 401 functions, 17 types
**Code generator:** mypyc (mypy compiler backend)

---

## Systematic mypyc Code-Generator Bugs

### Bug 1: `PyFloat_FromDouble` never NULL-checked (13 instances)

**Classification:** FIX — segfault on OOM
**Root cause:** mypyc's float boxing code path omits the `CPyError_OutOfMemory()` guard that is applied to `PyTuple_New` and `CPyTagged_StealAsObject`.

| Pattern | Sites | Crash mechanism |
|---------|-------|-----------------|
| `PyFloat_FromDouble` → `PyObject_Str(NULL)` | 3 (f-string formatting) | NULL deref in `PyObject_Str` |
| `PyFloat_FromDouble` → `PyObject_Vectorcall` args | 3 (`round()` calls) | NULL in args array |
| `PyFloat_FromDouble` → `PyTuple_SET_ITEM` | 4 (tuple construction) | NULL stored in tuple, deferred crash |
| `PyFloat_FromDouble` → list store/append | 3 (list building) | NULL in list, `CPy_DECREF(NULL)` |

**mypyc fix location:** The C code emitter for float-to-object boxing should emit:
```c
result = PyFloat_FromDouble(value);
if (unlikely(result == NULL)) CPyError_OutOfMemory();
```

*Found by: refcount-auditor, null-safety-scanner, error-path-analyzer (3 agents independently)*

### Bug 2: Missing `Py_DECREF(Py_TYPE(self))` in heap type deallocs (17 instances)

**Classification:** CONSIDER — monotonic type refcount leak
**Root cause:** `generate_dealloc_for_class()` in `mypyc/codegen/emitclass.py:943` emits `Py_TYPE(self)->tp_free((PyObject *)self)` but never emits the matching `Py_DECREF(tp)`.

Since these are heap types (created via `CPyType_FromTemplate` which uses `PyType_GenericAlloc`), `tp_alloc` INCREFs the type for each instance. Without the matching DECREF in dealloc, the type refcount grows by 1 per instance lifecycle.

**mypyc fix (3 lines in emitclass.py):**
```c
PyTypeObject *tp = Py_TYPE(self);
tp->tp_free((PyObject *)self);
Py_DECREF(tp);
```

*Found by: type-slot-checker, module-state-checker (2 agents)*

### Bug 3: Reference leak of `self` and `Py_None` in `tp_new` (10 instances)

**Classification:** FIX — object leak on `__init__` failure, `Py_None` leak on success
**Root cause:** mypyc's generated `tp_new` calls `__init__` inside `tp_new`. On failure, `self` is not DECREF'd. On success, the `Py_None` return from `__init__` is not DECREF'd.

```c
// Generated pattern (buggy):
PyObject *self = CPyDef_*_setup((PyObject*)type);
if (self == NULL) return NULL;
PyObject *ret = CPyPy_*___init__(self, args, kwds);
if (ret == NULL) return NULL;  // self leaked!
return self;                    // ret (Py_None) leaked!
```

The `Py_None` leak accumulates: `mess_ratio()` creates 10 plugin instances per call, leaking 10 `Py_None` references each time.

*Found by: error-path-analyzer, git-history-analyzer (2 agents)*

---

## Additional Findings

### Python source bug: Off-by-one in `encoding_unicode_range` (CONSIDER)

`cd.py:44` uses `range(0x40, 0xFF)` which misses byte 0xFF — same class of bug as the recently-fixed `cp_similarity` (`range(255)` → `range(256)`, commit `e1e2ccb`). Byte 0xFF maps to real characters in many encodings (e.g., 'ÿ' in Latin-1).

*Found by: git-history-analyzer*

### mypyc runtime concerns (POLICY)

| Concern | Impact |
|---------|--------|
| `#define Py_BUILD_CORE` hack to access 6 CPython internal headers | Fragile, may break in future CPython |
| 26 uses of `_Py_IDENTIFIER`/`_PyObject_GetAttrId` (internal APIs) | Not public, removed from headers in 3.13 |
| 294 private `_Py*` API uses across mypyc runtime | Per-version builds required |
| Single-phase init with cached module singleton | No subinterpreter support |
| `PyErr_Fetch`/`PyErr_Restore` (deprecated since 3.12) | Still functional, should migrate |
| Stable ABI fundamentally incompatible | mypyc performance model depends on CPython internals |

*Found by: version-compat-scanner, stable-abi-checker, module-state-checker*

---

## Scanner Accuracy on mypyc Code

| Scanner | Raw findings | True positives | False positive rate |
|---------|-------------|----------------|---------------------|
| Refcounts | 136 | 13 | 90.4% |
| Error paths | 402 | 6 | 98.5% |
| NULL safety | 25 | 13 | 48.0% |
| GIL | 17 | 0 | 100% |
| Module state | 89 | 1 | 98.9% |
| Type slots | 17 | 17 | 0% |
| External tools | 879 | 0 (NULL-related) | 100% |

The type slot scanner had the best accuracy — all 17 `heap_type_missing_type_decref` findings were true positives. The NULL safety scanner also performed well (48% FP rate). The error-path and refcount scanners had very high FP rates because mypyc's goto-label cleanup cascades and variable aliasing patterns are opaque to the scanners' heuristics.

---

## Architecture Assessment

| Aspect | Status |
|--------|--------|
| **Code generator** | mypyc (mypy compiler backend) |
| **Init style** | Single-phase with cached module singleton |
| **Types** | 17 heap types via `CPyType_FromTemplate` (static templates → runtime heap alloc) |
| **OOM strategy** | `CPyError_OutOfMemory()` calls `abort()` — except for `PyFloat_FromDouble` which is unguarded |
| **Stable ABI** | Fundamentally incompatible (mypyc runtime uses CPython internals) |
| **Free-threading** | Not supported (global state, no `Py_mod_gil` declaration) |
| **Complexity** | Well-structured: avg CC=6.0, max nesting=2, all hotspots are mechanical expansion |

---

## Priority Recommendations (for mypyc upstream)

1. **Bug 1 (PyFloat_FromDouble)** — Add `CPyError_OutOfMemory()` guard after `PyFloat_FromDouble` in the C code emitter, matching the existing pattern for `PyTuple_New`.

2. **Bug 3 (tp_new leaks)** — Add `Py_DECREF(self)` on `__init__` failure path, and `Py_DECREF(ret)` on success path, in `generate_new_for_class()`.

3. **Bug 2 (heap type dealloc)** — Add `Py_DECREF(tp)` after `tp->tp_free()` in `generate_dealloc_for_class()`.

4. **Off-by-one** (charset-normalizer) — Change `range(0x40, 0xFF)` to `range(0x40, 0x100)` in `cd.py:44`.
