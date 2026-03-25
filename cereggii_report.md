# cereggii C Extension Analysis Report

## Executive Summary

cereggii is a hand-written C extension (~5.7K lines, 265 functions, 16 files) providing lock-free atomic data structures for free-threaded Python: `AtomicDict`, `AtomicInt`, `AtomicRef`, `AtomicEvent`, and `ThreadHandle`. The codebase is exceptionally well-structured (average cyclomatic complexity 3.2, average function length 11.2 lines) and demonstrates strong expertise in lock-free programming with C11 atomics.

However, the analysis uncovered **significant reference counting issues** — the dominant bug class, with leaks affecting core operations like arithmetic, comparisons, `len()`, and reduce. Two **critical deadlock bugs** were found where `goto fail` skips `Py_END_CRITICAL_SECTION` on free-threaded Python — particularly dangerous for an extension whose primary purpose is free-threaded concurrency. The git history confirms this pattern: 7 of 20 recent fix commits address refcount bugs, and the fix-to-feature ratio is 1.33:1, indicating active correctness work.

**Total confirmed findings: 27 FIX, 14 CONSIDER, across all agents.**

The highest-priority issues are: (1) critical section deadlocks in `AtomicDict_init` and `BatchGetItem`, (2) reference leaks on every arithmetic operation via the `ATOMICINT64_BIN_OP` macro (12 instantiations + 6 unary ops), (3) reference leak on every `AtomicInt64` comparison, and (4) CAS retry loops in `GetAndUpdate`/`UpdateAndGet` that leak on every iteration.

---

**Project:** cereggii — Lock-free atomic data structures for free-threaded Python
**Source:** `~/projects/laruche/repositories/cereggii/src/cereggii`
**Stats:** 16 files, ~5.7K lines, 265 functions, 9 type definitions
**Designed for:** `Py_GIL_DISABLED` (free-threaded Python 3.13+), also supports 3.10+

---

## Critical Findings (FIX)

### 1. `goto fail` skips `Py_END_CRITICAL_SECTION` — deadlock on free-threaded Python (2 sites)

**Files:** `atomic_dict/lookup.c:207-284` (`BatchGetItem`), `atomic_dict/atomic_dict.c:236-260` (`AtomicDict_init`)

Multiple `goto fail` statements jump past `Py_END_CRITICAL_SECTION()`, leaving the per-object mutex permanently locked. Any subsequent operation on the same object will deadlock. In `BatchGetItem`, 4 error paths skip the end macro. In `AtomicDict_init`, both the hash-failure and table-overflow paths skip it.

*Found by: gil-discipline-checker, complexity-analyzer (2 confirmations)*

### 2. Reference leak on every `AtomicInt64` arithmetic operation (18 functions)

**File:** `atomic_int.c:568-731`

The `ATOMICINT64_BIN_OP` macro (12 instantiations: Add, Subtract, Multiply, etc.) and 6 unary operations (Negative, Positive, Absolute, Invert, Float, Index) all call `AtomicInt64_Get_callable()` which returns a new reference. This reference is passed to `PyNumber_*` but never DECREF'd. Leaks one `PyLong` per operation.

*Found by: git-history-analyzer*

### 3. Reference leak on every `AtomicInt64` comparison

**File:** `atomic_int.c:1092-1106`

`AtomicInt64_RichCompare` calls `AtomicInt64_Get_callable(self)` (new reference), passes it to `PyObject_RichCompare`, then returns without DECREF'ing `current`. Leaks on every `==`, `!=`, `<`, `>`, `<=`, `>=`.

*Found by: type-slot-checker*

### 4. CAS retry loops leak `py_current` and `py_desired` on every iteration

**Files:** `atomic_int.c:450-475` (`GetAndUpdate`), `atomic_int.c:493-518` (`UpdateAndGet`)

Inside `do...while` CAS loops, `PyLong_FromInt64` and `PyObject_CallOneArg` create new references each iteration. When CAS fails and the loop retries, previous values are overwritten without DECREF. Also leaks on the success path.

*Found by: refcount-auditor*

### 5. Leaked `approx_len` in `AtomicDict_LenBounds` — leaks on every call

**File:** `atomic_dict/atomic_dict.c:392-397`

`AtomicDict_ApproxLen()` returns a new reference passed to `Py_BuildValue("(OO)", ...)` with `O` format (which INCREFs). The original reference is never DECREF'd.

*Found by: refcount-auditor*

### 6. Leaked original `len` in `AtomicDict_Len_impl`

**File:** `atomic_dict/atomic_dict.c:474-483`

`PyNumber_InPlaceAdd` on immutable `int` returns a new object. The original `len` from `PyLong_FromSsize_t` is overwritten without DECREF. Also, the `PyNumber_InPlaceAdd` result is not NULL-checked — `PyLong_AsSsize_t(NULL)` will crash.

*Found by: refcount-auditor, null-safety-scanner, error-path-analyzer (3 confirmations)*

### 7. Leaked `ThreadHandle` on error paths in `GetHandle` (3 identical bugs)

**Files:** `atomic_dict/atomic_dict.c:618-631`, `atomic_ref.c:158-178`, `atomic_int.c:538-558`

If `ThreadHandle_init` fails, the `fail:` label only frees `args`, not `handle`. Additionally, `Py_BuildValue("(O)", self)` is not NULL-checked before passing to `ThreadHandle_init`.

*Found by: null-safety-scanner, refcount-auditor, error-path-analyzer (3 confirmations)*

### 8. Leaked `PyUnicode_FromFormat` in `PyErr_SetObject` (5 sites)

**Files:** `atomic_int.c:41,67,103,130` (overflow errors), `atomic_dict/insert.c:324` (expectation failed)

`PyErr_SetObject` does NOT steal its second argument. The `PyUnicode_FromFormat` return value is passed directly without storing it, so it can never be DECREF'd. Leaks a string on every overflow/expectation error.

*Found by: refcount-auditor, null-safety-scanner, error-path-analyzer (3 confirmations)*

### 9. Nested `Py_BuildValue` leaks + spurious `Py_INCREF` in `AtomicDict_Debug`

**File:** `atomic_dict/atomic_dict.c:526-528,576-577`

Inner `Py_BuildValue` calls return new references passed via `O` format (which INCREFs again). The originals leak. Additionally, `Py_INCREF(key)` and `Py_INCREF(value)` after `Py_BuildValue` with `O` format add extra references that are never released.

*Found by: refcount-auditor, null-safety-scanner, complexity-analyzer, git-history-analyzer (4 confirmations)*

### 10. Missing cleanup in `flush_one` error path

**File:** `atomic_dict/insert.c:363-422`

The `fail:` path returns -1 without DECREF'ing any of the INCREF'd references (`key`, `expected`, `new`, `desired`, `current`, `previous`). Leaks multiple objects on any reduce flush failure.

*Found by: refcount-auditor, complexity-analyzer*

### 11. `AtomicDictFastIterator` missing GC flag, traverse, and clear

**File:** `cereggii.c:212-222`

The iterator holds strong references to `AtomicDict` and `AtomicDictMeta` (both `PyObject*` members) but has no `Py_TPFLAGS_HAVE_GC`, no `tp_traverse`, no `tp_clear`. The cyclic GC cannot see these references. Cycles through iterators are uncollectable.

*Found by: type-slot-checker*

### 12. Incorrect `Py_DECREF` on static types and exception objects in module init (7 sites)

**File:** `cereggii.c:440-464`

`Py_DECREF` after `PyModule_AddObjectRef` on static types is an unbalanced decrement. For exception objects, it makes the global variable a borrowed reference. Currently masked by immortal objects on 3.12+.

*Found by: module-state-checker, refcount-auditor, version-compat-scanner (3 confirmations)*

### 13. `meta_init_pages` and `meta_copy_pages` return -1 without `PyErr_NoMemory`

**File:** `atomic_dict/meta.c:79,117`

`PyMem_RawMalloc` doesn't set Python exceptions. These functions return -1 on failure without calling `PyErr_NoMemory()`, causing `SystemError` when the error propagates.

*Found by: error-path-analyzer*

### 14. `PyThread_tss_set` failure returns NULL without exception

**File:** `atomic_dict/accessor_storage.c:29-31`

Returns NULL to callers without setting any Python exception, causing `SystemError`.

*Found by: error-path-analyzer*

### 15. Unchecked `PyObject_IsTrue` return (-1 = error) in `reduce_specialized_and/or`

**File:** `atomic_dict/insert.c:627-632,670`

`PyObject_IsTrue` returns -1 on error, which is truthy in C. The error is silently swallowed and a wrong result is returned with a pending exception.

*Found by: git-history-analyzer*

### 16. Leaked `value` in `AtomicDict_Reduce_impl` loop

**File:** `atomic_dict/insert.c:502-529`

`value` is INCREF'd from `PyTuple_GetItem` but never DECREF'd in the loop body. `key` is stolen by `reduce_table_set`, but `value` leaks on every iteration.

*Found by: git-history-analyzer*

### 17. Exception clobbering after `PyObject_GetIter`

**File:** `atomic_dict/insert.c:476-479`

`PyErr_Format` overwrites the more informative `TypeError` already set by `PyObject_GetIter`.

*Found by: error-path-analyzer*

### 18. `builtins` reference leak on error path (3.13+ only)

**File:** `atomic_dict/insert.c:891-901`

`PyEval_GetFrameBuiltins` returns a new reference on 3.13+. If `PyDict_GetItemStringRef` fails, `builtins` is leaked because the `fail` label doesn't clean it up.

*Found by: version-compat-scanner, refcount-auditor*

---

## Important Findings (CONSIDER)

| # | Finding | File | Source |
|---|---------|------|--------|
| 1 | `AtomicDictPage` missing GC flag (works via manual traversal) | cereggii.c:201 | type-slot |
| 2 | `AtomicRef_Set` double-decref race potential | atomic_ref.c:89-107 | gil |
| 3 | `get_meta` reads `_Atomic` without explicit `atomic_load` | accessor_storage.c:90 | gil |
| 4 | Unchecked `mtx_init`/`cnd_init` return values | atomic_event.c:22-23 | null, exttools |
| 5 | 3 deprecated `PyObject_CallObject` calls | meta.c:36,39,42 | version-compat |
| 6 | `reduce_table_free` doesn't DECREF stored objects on error | reduce_table.h:82-91 | git-history |
| 7 | `ATOMICINT64_BIN_OP` unchecked `PyObject_IsInstance` (-1 error) | atomic_int.c:572 | error-path |
| 8 | `CereggiiConstant_Type` has no `tp_dealloc` (intentional) | cereggii.c:253 | type-slot |
| 9 | Module init `Py_DECREF` on static types (masked on 3.12+) | cereggii.c:448-464 | module-state |

---

## Architecture & Migration Assessment

| Aspect | Status |
|--------|--------|
| **Init style** | Single-phase (`m_size=-1`) |
| **Static types** | 9 |
| **Global PyObject state** | 5 (3 sentinels + 2 exceptions, sentinels immortalized) |
| **Multi-phase init migration** | HIGH difficulty (9 types, 117 sentinel comparison sites) |
| **Stable ABI** | Not feasible (re-implements CPython internal biased refcounting) |
| **Free-threading support** | Core design — uses C11 atomics, `_Py_TryIncrefFast`, `PyMutex` |
| **CPython internals dependency** | Heavy (`ob_ref_local`, `ob_ref_shared`, `_Py_ThreadId`, `_Py_IMMORTAL_REFCNT_LOCAL`) |
| **Complexity** | Excellent (avg 3.2 cyclomatic, 11.2 lines/function, 1 hotspot) |

---

## Priority Recommendations

1. **Finding 1 (critical section deadlocks)** is the highest priority — these will deadlock on the exact platform cereggii is designed for (free-threaded Python). Fix by adding `Py_END_CRITICAL_SECTION()` before every `goto` that exits the scope.

2. **Findings 2-4, 16 (arithmetic/comparison/CAS reference leaks)** affect every use of `AtomicInt64` arithmetic and comparisons. Fix all 18 `ATOMICINT64_BIN_OP` instantiations + 6 unary ops + `RichCompare` + CAS loops by storing the `Get_callable` result and DECREF'ing after use.

3. **Findings 6, 10 (len/flush leaks)** affect core `AtomicDict` operations. Fix `Len_impl` by saving the old `len` before `PyNumber_InPlaceAdd`. Fix `flush_one` by adding XDECREF calls to the `fail` label.

4. **Finding 8 (PyErr_SetObject leaks)** — replace `PyErr_SetObject(exc, PyUnicode_FromFormat(...))` with `PyErr_Format(exc, ...)` which handles cleanup internally.

5. **Finding 11 (iterator missing GC)** — add `Py_TPFLAGS_HAVE_GC`, `tp_traverse`, `tp_clear`, change to `PyObject_GC_New/GC_Del`.

6. **Finding 12 (module init Py_DECREF)** — delete the 5 `Py_DECREF` calls on static type objects, keep the ones on dynamically-created constants/exceptions.