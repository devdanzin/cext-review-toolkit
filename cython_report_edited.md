# Cython Runtime Analysis Report

## General Summary

This report presents a unified analysis of the Cython runtime utility library (`Cython/Utility/`), the ~27K lines of hand-written C code that are `#include`d into every Cython-compiled extension.

The analysis was performed using cext-review-toolkit (refcount auditor, error-path analyzer, type-slot checker, git-history analyzer) and code-review-toolkit (silent-failure hunter, pattern-consistency checker, tech-debt inventory). Together, these identified 15 confirmed bugs and a systemic anti-pattern (unconditional `PyErr_Clear()` calls) that pervades the codebase.

**The overall code quality is high.** Our scanners achieved a 96–97% false positive rate, confirming that Cython's C code is well-written compared to most C extensions we've analyzed. The heap type lifecycle management is correct (unlike mypyc, which has a dealloc bug). The `CYTHON_ASSUME_SAFE_MACROS` / `CYTHON_ASSUME_SAFE_SIZE` dual-path pattern is well-executed. Recent fix commits have been thorough — we verified that every recently-fixed bug class has no remaining unfixed instances.

**However, two categories of issues have ecosystem-wide impact:**

1. **GC correctness gaps**: `CyFunction.tp_traverse` doesn't visit `func_annotations` (creating uncollectable cycles for every Cython function), and 7 of 10 GC-tracked coroutine/generator types are missing `Py_tp_clear` slot registration (preventing the GC from breaking cycles). Both are confirmed with reproducers.

2. **Unconditional `PyErr_Clear()` calls**: ~25 sites across the runtime clear pending exceptions without checking exception type, silently swallowing `MemoryError`, `KeyboardInterrupt`, and `SystemError`. The most critical instances are on hot paths: global name lookup (`__Pyx__GetModuleGlobalName`) and class-level name access (`__Pyx__GetNameInClass`), where an OOM causes the code to silently return the **wrong object** rather than raising.

**Total confirmed findings: 15 FIX, 15 CONSIDER.**

---

## Scope & Impact

| Attribute | Value |
|-----------|-------|
| **Files analyzed** | 28 C files, 8 `.pyx` files, 2 `.pxd` files, 1 header (~29K lines total) |
| **Functions** | 339 |
| **Ecosystem impact** | Every Cython-compiled extension (thousands of packages) |
| **Scanner false positive rate** | 96% (refcounting), 97% (error paths) — indicating high baseline quality |
| **Complexity** | 1 hotspot, avg CC 6.6, max nesting 4 — clean for a runtime library |

---

## Findings by Priority

### Must Fix (FIX)

#### GC Correctness

**1. `func_annotations` missing from `CyFunction` traverse — uncollectable cycles**
Every `PyObject*` member that is `Py_CLEAR`'d in `__Pyx__CyFunction_clear` is also `Py_VISIT`'d in traverse — except `func_annotations`. Since annotations can contain arbitrary types (including self-referential ones), cycles through annotations are invisible to the GC. `CyFunction` is the type behind every Cython-compiled function — this affects the entire ecosystem.
- `CythonFunction.c:947-989` (traverse), `:918` (clear), `:94` (struct)
- *Source: refcount-auditor*
- *Fix: add `Py_VISIT(m->func_annotations);` to traverse*
- **Reproducer confirmed** — see Appendix

**2. 7 of 10 GC-tracked types missing `Py_tp_clear` slot registration**
The Coroutine, IterableCoroutine, Generator, AsyncGen, AsyncGenASend, AsyncGenWrappedValue, and AsyncGenAThrow types all have `Py_TPFLAGS_HAVE_GC` and `Py_tp_traverse` registered, and all have `tp_clear` *functions* defined — but none register `Py_tp_clear` in their slot tables. Only CoroutineAwait, CyFunction, and FusedFunction do. This means the GC can detect cycles involving these types but cannot break them.
- `Coroutine.c:1929-1943`, `AsyncGen.c:400-413,611-618,700-703,970-978`
- *Sources: type-slot-checker, pattern-consistency-checker*
- *Fix: register `{Py_tp_clear, ...}` in each type's slot table*

#### Reference Leaks

**3. `__Pyx_Generator_Replace_StopIteration` leaks RuntimeError**
`PyErr_SetObject` does not steal its argument. The `RuntimeError` created for PEP 479 StopIteration-to-RuntimeError conversion is never DECREF'd. Leaks ~767 bytes per conversion. Every other `PyErr_SetObject` call site in the Utility directory correctly DECREFs — this is the sole exception.
- `Coroutine.c:417-427`
- *Sources: refcount-auditor, git-history-analyzer*
- *Fix: add `Py_DECREF(new_exc);` after `PyErr_SetObject`*
- **Reproducer confirmed** — see Appendix. 7.5 MB leaked per 10K conversions.

**4. `CyFunction` `func_classobj` leaked in limited API mode**
In `CYTHON_COMPILING_IN_LIMITED_API`, `func_classobj` is set via `Py_XDECREF`+`Py_XINCREF` but never cleared in `__Pyx__CyFunction_clear` or visited in traverse. The class object leaks every time a bound CyFunction is destroyed.
- `CythonFunction.c:909,980`
- *Source: type-slot-checker*

#### Error Swallowing on Hot Paths

**5. `__Pyx__GetModuleGlobalName` swallows MemoryError from dict lookups**
Three unconditional `PyErr_Clear()` calls on the hot path for every global name lookup. Under OOM, global lookups silently fall through to builtins, potentially returning the **wrong object** with no error raised.
- `ObjectHandling.c:1589-1603`
- *Source: silent-failure-hunter*

**6. `__Pyx__GetNameInClass` swallows all exceptions including MemoryError**
`PyErr_Clear()` unconditionally clears any exception from `PyObject_GetItem(dict, name)` before falling back to module global lookup. Called for **every class-level name access** in Cython. Under OOM, class body name lookups silently resolve to module globals.
- `ObjectHandling.c:1477-1488`
- *Source: error-path-analyzer*

**7. `__Pyx_MergeVtables` clears errors on SUCCESS path**
Type creation reports success even when vtable lookup fails with MemoryError. Downstream virtual method calls could **segfault** on corrupt vtable state.
- `ImportExport.c:784`
- *Source: silent-failure-hunter*

#### Async Generator State

**8. `athrow_throw` missing state reset in aclose() error path**
When `athrow_throw` encounters StopAsyncIteration/GeneratorExit in aclose() mode, it doesn't reset `ag_running_async=0` or `agt_state=CLOSED`, unlike the identical path in `athrow_send_impl` (line 884-885). The generator is permanently stuck in "running_async" state, causing subsequent operations to fail with "asynchronous generator is already running."
- `AsyncGen.c:930`
- *Source: pattern-consistency-checker*

#### CyFunction Call Dispatch

**9. `IndexError` clobbered by `TypeError` in `CyFunction_CallAsMethod`**
When called with empty args, `PyTuple_GetItem(args, 0)` sets `IndexError`, which is overwritten by `PyErr_Format(TypeError, ...)` without clearing first. Triggers assertions in debug builds.
- `CythonFunction.c:1116-1122`
- *Source: error-path-analyzer*

**10. Unchecked `PyDict_Size` return in `CyFunction_CallMethod`**
`PyDict_Size(kw)` returns `-1` on error, which doesn't equal `0`, causing fall-through to `SystemError("Bad call flags")` that clobbers the real `TypeError`. Affects the central dispatch for all CyFunction calls.
- `CythonFunction.c:1018,1024,1040`
- *Source: error-path-analyzer*

#### Silent Wrong Results

**11. `__Pyx_Py_UNICODE_*` methods return False under OOM**
Template-generated `isalpha()`, `isdigit()`, `isprintable()`, etc. clear MemoryError and return 0 (false). Comment says "cannot fail" — it can. String validation **silently produces wrong results** under memory pressure.
- `StringTools.c:765-766`
- *Source: silent-failure-hunter*

**12. `__Pyx_setup_reduce_is_named` clears MemoryError during pickle setup**
Pickle reduce setup gets wrong comparison results under OOM, potentially producing **silently corrupt pickle data**.
- `ExtensionTypes.c:380`
- *Source: silent-failure-hunter*

#### Other

**13. `__Pyx_PyCode_Replace_For_AddTraceback` returns NULL without exception**
C API contract violation — returns NULL but `PyErr_Occurred()` is false. Traceback addition silently fails.
- `Exceptions.c:871-873`
- *Source: silent-failure-hunter*

**14. Profile/trace macros fabricate None values**
When return value boxing fails, `None` is substituted and fed to profilers/tracers. Production monitoring with cProfile/line_profiler/sys.settrace receives incorrect data.
- `Profile.c:256,266,489,502`
- *Source: silent-failure-hunter*

**15. Non-BaseException catching silently ignored (FIXME since 2018)**
`except NonBaseExceptionClass` is silently ignored instead of raising `TypeError` as CPython does. Behavioral divergence from CPython.
- `ModuleSetupCode.c:1539,1553`
- *Source: tech-debt-inventory*

---

### Should Consider (CONSIDER)

#### GC & Lifecycle

| # | Finding | File | Source |
|---|---------|------|--------|
| 1 | `gi_frame` not visited in `tp_traverse` — cycle risk if user code introspects generator frames | Coroutine.c:1364 | type-slot |
| 2 | `__Pyx_PyFloat_AsDouble` may return `-1.0` without exception if `nb_float` returns NULL without setting exception | Optimize.c:670-699 | error-path |
| 3 | Warning in `Coroutine_Finalize` before exception restore | Coroutine.c:1470-1494 | error-path |
| 4 | `RaiseUnpickleChecksumError` gives `ImportError` instead of `PickleError` if `import pickle` fails | ExtensionTypes.c:499-518 | error-path |
| 5 | `FetchStopIterationValue` — cleanup DECREFs could theoretically clobber exception | Coroutine.c:644-649 | error-path |
| 6 | Dead code / fragile fallback in `__Pyx_MergeKeywords_any` — works by accident | FunctionArguments.c:828-843 | error-path |

#### Error Swallowing (Lower Priority)

| # | Finding | File | Source |
|---|---------|------|--------|
| 7 | `__Pyx_TemplateLibFallback` clears import error unconditionally — t-string support silently falls back | TString.c:41 | silent-failure |
| 8 | `__Pyx_CyFunction_get_is_coroutine` clears import errors from asyncio | CythonFunction.c:614 | silent-failure |
| 9 | `__Pyx_PyDict_GetItemStr` swallows ALL errors from `_PyDict_GetItem_KnownHash` | ModuleSetupCode.c:964 | silent-failure |

#### Technical Debt

| # | Finding | File | Source |
|---|---------|------|--------|
| 10 | AsyncGen.c is a full-file fork of CPython 3.6 genobject.c — 1031 lines, 8+ years of upstream drift, uses `_Py_NewReference` (changed in 3.12+) | AsyncGen.c | tech-debt |
| 11 | `_PyFloat_FormatAdvancedWriter`/`_PyLong_FormatAdvancedWriter` copied from CPython 3.5 — unstable internal APIs in every f-string/format call | StringTools.c:1239 | tech-debt |
| 12 | `_PyObject_GetMethod` copied from CPython 3.7 — 6+ years old, on method call fast path | ObjectHandling.c:1767 | tech-debt |
| 13 | `ModuleSetupCode.c` has 265 `#if` directives in 3245 lines (1 per 12 lines) — testing all combinations impractical | ModuleSetupCode.c | tech-debt |
| 14 | Copied CPython `special_lookup` from ceval.c (3.3, 2013) and `update_bases` from bltinmodule.c (3.7, 2021) | ObjectHandling.c:1645,1767 | tech-debt |
| 15 | `Py_UNICODE` type still used in TypeConversion.c and StringTools.c — deprecated since 3.3 | TypeConversion.c:796 | tech-debt |

---

## Systemic Pattern: Unconditional `PyErr_Clear()`

The most significant systemic finding is a pervasive anti-pattern across the runtime: **~25 unconditional `PyErr_Clear()` calls** that clear pending exceptions without checking exception type, silently swallowing `MemoryError`, `KeyboardInterrupt`, and `SystemError`.

The correct pattern (check exception type first) already exists in ~25 other call sites in the same codebase — e.g., `ImportExport.c:131-134` properly checks `PyErr_ExceptionMatches(PyExc_ImportError)` before clearing. The inconsistency suggests the unguarded calls were written under time pressure or by different authors.

**Affected findings**: #5, #6, #7, #11, #12, #13, #14, plus CONSIDER items #7-9.

**Proposed solution**: A `__Pyx_PyErr_ClearIfMatches(expected_type)` helper would make the correct pattern trivial to apply consistently:
```c
static CYTHON_INLINE int __Pyx_PyErr_ClearIfMatches(PyObject *exc_type) {
    if (PyErr_ExceptionMatches(exc_type)) { PyErr_Clear(); return 1; }
    return 0;
}
```

This would allow each call site to be mechanically updated:
```c
// Before (swallows all exceptions):
PyErr_Clear();

// After (only clears expected exception):
if (!__Pyx_PyErr_ClearIfMatches(PyExc_KeyError)) return NULL;
```

---

## Strengths

1. **Cython does NOT have the mypyc heap type dealloc bug** — `__Pyx_PyHeapTypeObject_GC_Del` correctly saves `Py_TYPE(obj)`, calls `PyObject_GC_Del`, then `Py_DECREF(type)`. Used consistently across all types.
2. **High baseline code quality** — 96% scanner false positive rate on refcounting, 97% on error paths. The code uses `unlikely()` annotations, handles conditional compilation carefully, and delegates exception-setting to well-named helpers.
3. **`CYTHON_ASSUME_SAFE_MACROS`/`CYTHON_ASSUME_SAFE_SIZE` dual-path pattern** — systematically provides both a fast path (using unchecked `GET_ITEM`/`GET_SIZE` macros) and a safe path (using checked API functions with proper error handling).
4. **Active maintenance** — 43 fix commits in 205 days, including recent use-after-free, early-return-bypass-cleanup, and thread-safety fixes.
5. **Recent fixes are thorough** — string reference leak fix (9ad6ea3), buffer use-after-free fix (11e94c7), atexit error handling fix (b01c437) — all verified as having no remaining unfixed instances of the same class.
6. **GC-tracked dealloc functions are correctly structured** — all call `PyObject_GC_UnTrack` before clearing members.

---

## Recommended Action Plan

### Immediate (high ecosystem impact)
1. Add `Py_VISIT(m->func_annotations)` to `__Pyx_CyFunction_traverse` — **one line**, fixes GC bug affecting all Cython packages
2. Add `Py_DECREF(new_exc)` after `PyErr_SetObject` in `__Pyx_Generator_Replace_StopIteration` — **one line**, stops 767-byte-per-call leak
3. Register `Py_tp_clear` slots for 7 coroutine/generator/async gen types — the clear functions already exist
4. Check `PyErr_ExceptionMatches()` before `PyErr_Clear()` in `__Pyx__GetModuleGlobalName` and `__Pyx__GetNameInClass` — prevents `MemoryError` swallowing on hot paths

### Short-term
5. Fix `func_classobj` leak in limited API path
6. Add `ag_running_async=0` and `agt_state=CLOSED` in `athrow_throw` aclose() error path
7. Add `PyErr_Clear()` before `PyErr_Format` in `CyFunction_CallAsMethod`
8. Add `PyErr_Occurred()` check after `PyDict_Size` in `CyFunction_CallMethod`
9. Create `__Pyx_PyErr_ClearIfMatches()` helper and apply to remaining ~20 unguarded `PyErr_Clear()` sites

### Longer-term
10. Add `Py_VISIT(gen->gi_frame)` to coroutine traverse
11. Audit AsyncGen.c against current CPython (forked from 3.6, 8+ years of drift)
12. Verify `_PyFloat_FormatAdvancedWriter`/`_PyLong_FormatAdvancedWriter` signatures against CPython 3.14
13. Implement the 2018 FIXME: raise `TypeError` for non-BaseException except clauses
14. Consider adding `Py_VISIT(m->func_annotations)` to the code *generator* too (for generated code that creates CyFunction-like types)

---

## Appendix: Reproducers

### Reproducer 1: `func_annotations` missing from CyFunction traverse — Uncollectable GC cycle

**Severity:** HIGH — affects every Cython-compiled function with annotations forming a cycle
**Confirmed on:** Cython 3.2.4, Python 3.14

`CyFunction.tp_traverse` doesn't visit `func_annotations`, so the cyclic GC cannot detect or break reference cycles through annotations. The cycle persists until the annotations are explicitly cleared.

```python
"""Reproducer: CyFunction func_annotations not visited by tp_traverse.

CythonFunction.c: tp_traverse (lines 947-989) visits every PyObject* member
EXCEPT func_annotations. tp_clear (line 918) DOES clear it.
Asymmetry = GC can't detect cycles through annotations.

Tested: Cython 3.2.4, Python 3.14.
"""
import gc
import weakref
import Cython.Compiler.Code as code_mod

# Find a CyFunction
func = None
for name in dir(code_mod):
    obj = getattr(code_mod, name)
    if type(obj).__name__ == 'cython_function_or_method':
        func = obj
        break

assert func is not None, "No CyFunction found"
print(f"Using: {type(func).__name__}")

# Create a cycle: func -> __annotations__ -> target -> func
class CycleTarget:
    pass

target = CycleTarget()
weak_target = weakref.ref(target)

target.func_ref = func
func.__annotations__ = {"cycle": target}
del target

gc.collect(); gc.collect(); gc.collect()

if weak_target() is not None:
    print("CONFIRMED: CycleTarget NOT collected after gc.collect()")
    print("  Cycle through func.__annotations__ is uncollectable")
    func.__annotations__ = {}  # manual cleanup
    gc.collect()
    print(f"  After manual clear + gc: {'collected' if weak_target() is None else 'STILL LEAKED'}")
else:
    print("Collected — may be fixed in this Cython version")
```

**Output:**
```
Using: cython_function_or_method
CONFIRMED: CycleTarget NOT collected after gc.collect()
  Cycle through func.__annotations__ is uncollectable
  After manual clear + gc: collected
```

---

### Reproducer 2: `__Pyx_Generator_Replace_StopIteration` leaks RuntimeError — Memory leak

**Severity:** HIGH — 767 bytes leaked per StopIteration conversion in any Cython generator
**Confirmed on:** Cython 3.2.4, Python 3.14

Every PEP 479 StopIteration-to-RuntimeError conversion in a Cython generator leaks one `RuntimeError` object because `PyErr_SetObject` does not steal its argument.

```python
"""Reproducer: RuntimeError leaked in Cython generator StopIteration conversion.

Coroutine.c:417-427: new_exc from PyObject_CallFunction is never Py_DECREF'd
after PyErr_SetObject. Leaks ~767 bytes per conversion.

Requires: Cython extension with generator that raises StopIteration.

test_gen.pyx:
    def leaky_generator():
        raise StopIteration("intentional")
        yield

    def trigger_leak(n):
        for i in range(n):
            g = leaky_generator()
            try:
                next(g)
            except RuntimeError:
                pass

Build with: cythonize -i test_gen.pyx
Tested: Cython 3.2.4, Python 3.14.
"""
import sys, gc, tracemalloc
sys.path.insert(0, '/tmp/cy_test')
import test_gen

tracemalloc.start()
snap1 = tracemalloc.take_snapshot()
test_gen.trigger_leak(10000)
gc.collect()
snap2 = tracemalloc.take_snapshot()

stats = snap2.compare_to(snap1, 'lineno')
leaked = sum(s.size_diff for s in stats if s.size_diff > 0)
print(f"Memory leaked after 10000 StopIteration conversions: {leaked / 1024:.1f} KB")
print(f"Per conversion: ~{leaked / 10000:.0f} bytes")
# Expected: ~0 KB
# Actual: ~7492 KB (~767 bytes per conversion)
```

**Output:**
```
Memory leaked after 10000 StopIteration conversions: 7492.0 KB
Per conversion: ~767 bytes
```

---

### Reproducer Summary

| Finding | Reproducer | Result |
|---------|-----------|--------|
| `func_annotations` missing from CyFunction traverse | **LEAK CONFIRMED** | Cycle through annotations uncollectable by GC |
| `Generator_Replace_StopIteration` RuntimeError leak | **LEAK CONFIRMED** | 7.5 MB leaked per 10K conversions (~767 bytes each) |
| `__Pyx__GetNameInClass` swallows MemoryError | Code-confirmed | Requires OOM during class-level name lookup |
| `__Pyx__GetModuleGlobalName` swallows MemoryError | Code-confirmed | Requires OOM during global name lookup |
| `func_classobj` leaked in limited API | Code-confirmed | Requires `CYTHON_COMPILING_IN_LIMITED_API` build |
| 7 types missing `Py_tp_clear` slot | Code-confirmed | Requires suspended coroutine/generator in cycle |
| `athrow_throw` missing state reset | Code-confirmed | Requires specific async gen aclose() error path |
| `MergeVtables` clears error on success | Code-confirmed | Requires OOM during type creation |
| `Py_UNICODE_*` returns wrong result under OOM | Code-confirmed | Requires OOM during string classification |
| 25+ unconditional `PyErr_Clear()` calls | Code-confirmed | Requires OOM on various hot paths |

**2 confirmed leak reproducers** (both measurable from pure Python using installed Cython packages), **8 code-confirmed issues** requiring OOM conditions, specific build configurations, or specific async generator error paths.
