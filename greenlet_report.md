# C Extension Analysis Report

## Extension: greenlet/_greenlet
## Scope: ~/projects/laruche/repositories/greenlet/src/greenlet/ (full deep analysis)
## Agents Run: refcount-auditor, error-path-analyzer, null-safety-scanner, gil-discipline-checker, resource-lifecycle-checker, module-state-checker, type-slot-checker, pyerr-clear-auditor, stable-abi-checker, version-compat-scanner, c-complexity-analyzer, git-history-analyzer (12/12)

## Executive Summary

Greenlet is a remarkably well-engineered C++ extension for its extraordinary complexity — cooperative coroutine switching via direct C stack manipulation. The RAII wrapper architecture (`OwnedObject`, `BorrowedObject`, `Require()`) prevents most common C extension bug classes. However, the analysis found **5 confirmed FIX-level bugs**: a `PyAddObject` wrong-variable decref (confirmed by 3 agents independently), a `delete_later` double-decref on Python 3.13+ during greenlet switching, missing `c_stack_refs` initialization for free-threaded Python 3.14, incorrect `c_recursion_depth` computation on Python 3.12-3.13 (with existing reviewer XXX comment confirming it), and an unchecked `PyLong_FromSsize_t` that crashes on OOM. The most impactful is the `delete_later` double-decref which affects every greenlet switch where the trashcan mechanism is active on 3.13+. Free-threading readiness is advanced but has a data race on the `deleteme` vector.

## Extension Profile
- Module: _greenlet (16 C/C++ files, ~4,400 lines hand-written C++)
- Init style: single-phase (`PyInit__greenlet`)
- Python targets: >=3.10
- Limited API: no (architecturally incompatible — requires `PyThreadState` internals)
- Types defined: 2 (`PyGreenlet_Type`, `PyGreenletUnswitchable_Type`)
- External tools: 137 clang-tidy + 1 cppcheck findings (106 warnings, 32 errors)

## Key Metrics

| Dimension | Status | FIX | CONSIDER | Top Finding |
|-----------|--------|-----|----------|-------------|
| Refcount Safety | Y | 2 | 2 | `delete_later` double-decref on 3.13+ switches |
| Error Handling | Y | 3 | 3 | `PyAddObject` decrefs module instead of new_object |
| NULL Safety | Y | 2 | 4 | Debug-build crash in `PyErrOccurred::from_current()` |
| GIL Discipline | G | 0 | 8 | `deleteme` vector data race on free-threaded builds |
| Module State | Y | 1 | 7 | `PyAddObject` wrong decref (same as error handling) |
| Type Slots | G | 0 | 4 | `delete_later` not visited in `tp_traverse` on 3.13+ |
| ABI Compliance | G | 0 | 0 | Not feasible — requires `PyThreadState` internals |
| Version Compat | G | 0 | 10 | 7 dead compat blocks, `PyModule_AddObject` deprecated |
| PyErr_Clear | G | 0 | 0 | Only 2 calls, both correct `WriteUnraisable` pattern |
| Resources | G | 0 | 2 | Module init leak on error; `tp_new` leak on throw |
| Complexity | Y | 0 | 3 | `inner_bootstrap` (9.5), `~ThreadState` (9.0) |
| Git History | R | 2 | 3 | Missing `c_stack_refs` init; wrong `c_recursion_depth` |

G = No FIX findings | Y = 1-3 FIX findings | R = 4+ FIX findings

## Findings by Priority

### Must Fix (FIX)

**1. `PyAddObject` error path decrefs module instead of new_object**
- **Location**: `greenlet_refs.hpp:918`
- **Agents**: refcount-auditor, error-path-analyzer, module-state-checker (independently confirmed)
- **Impact**: On `PyModule_AddObject` failure, the module gets an erroneous DECREF (potential use-after-free) while the added object's reference leaks
- **Fix**: Change `Py_DECREF(p)` to `Py_DECREF(new_object)`, or replace entire method with `PyModule_AddObjectRef` (available on all supported versions)

**2. `delete_later` double-decref on Python 3.13+ during greenlet restore**
- **Location**: `TPythonState.cpp:259-262`
- **Agent**: refcount-auditor
- **Impact**: Every greenlet switch where `tstate->delete_later` is non-NULL drops an extra reference, causing premature object destruction or use-after-free. Triggered by CPython's trashcan mechanism during container deallocation
- **Fix**: Replace `Py_CLEAR(this->delete_later)` with `this->delete_later = nullptr` (transfer ownership without decrementing)

**3. Missing `c_stack_refs` initialization in `set_initial_state` for free-threaded Python 3.14**
- **Location**: `TPythonState.cpp:286-304`
- **Agent**: git-history-analyzer (similar bug detection from commit b54c4bd)
- **Impact**: New greenlets on free-threaded 3.14 start with stale/NULL `c_stack_refs`, which gets restored to the thread state on first switch-away. Also missing `stackpointer` initialization
- **Fix**: Add `c_stack_refs` and `stackpointer` initialization matching `operator<<`

**4. Incorrect `c_recursion_depth` computation in `set_initial_state` (Python 3.12-3.13)**
- **Location**: `TPythonState.cpp:298`
- **Agent**: git-history-analyzer (code has existing reviewer XXX comment)
- **Impact**: Uses `py_recursion_limit - py_recursion_remaining` (Python recursion depth) instead of `Py_C_RECURSION_LIMIT - c_recursion_remaining` (C recursion depth). Could cause premature recursion errors or allow too-deep C recursion in new greenlets
- **Fix**: `this->c_recursion_depth = Py_C_RECURSION_LIMIT - tstate->c_recursion_remaining;`

**5. Unchecked `PyLong_FromSsize_t` crashes on OOM in module init**
- **Location**: `greenlet.cpp:232`
- **Agents**: error-path-analyzer, null-safety-scanner
- **Impact**: If `PyLong_FromSsize_t(CLOCKS_PER_SEC)` returns NULL, `Py_INCREF(NULL)` in `PyAddObject` crashes
- **Fix**: Wrap in `Require()`: `OwnedObject::consuming(Require(PyLong_FromSsize_t(CLOCKS_PER_SEC)))`

### Should Consider (CONSIDER)

**6. `deleteme` vector data race on free-threaded builds** (GIL discipline)
- `TThreadState.hpp:283-314, 343-347` — `delete_when_thread_running` pushes to another thread's `deleteme` vector without synchronization while `clear_deleteme_list` reads/clears it

**7. `tp_is_gc` guard missing on `PyGreenletUnswitchable_Type`** (type slots)
- `PyGreenletUnswitchable.cpp:143` — Sets `tp_is_gc` unconditionally while base type conditionally excludes it on `Py_GIL_DISABLED`, causing mimalloc GC assertion failures

**8. `delete_later` not visited in `tp_traverse` on Python 3.13+** (type slots)
- `TPythonState.cpp:306-334` — Owned `PyObject*` not visible to GC, could prevent cycle detection

**9. Borrowed reference to `sys.stderr` across arbitrary Python code** (refcount)
- `PyGreenlet.cpp:242-248` — `PySys_GetObject` returns borrowed ref used across `PyFile_WriteObject` which runs `__repr__`

**10. `PyModule_AddObject` should be replaced with `PyModule_AddObjectRef`** (version compat)
- `greenlet_refs.hpp:911-921` — Would eliminate the bug-prone manual INCREF/DECREF dance

**11. `PyErr_Fetch`/`Restore` deprecated in 3.12** (version compat)
- ~5 call sites across `greenlet_refs.hpp`, `greenlet_exceptions.hpp`, `TGreenlet.cpp`

**12. Migrate dealloc to PEP 442 `tp_finalizer`** (complexity)
- `PyGreenlet.cpp:189-294` — Existing TODO comment; would eliminate fragile manual resurrection pattern

### Tensions

- **Module state vs. architecture**: module-state-checker flags single-phase init and 9+ global state items. But greenlet's thread-local `ThreadState` is fundamentally per-thread, not per-interpreter. Multi-phase init migration would be a major architectural redesign with questionable benefit (greenlets are tied to C stacks which are process-global). **Recommendation**: Keep current init style; focus on fixing actual bugs.

- **`tp_is_gc` for active greenlets**: On GIL builds, active greenlets report as non-collectable, potentially leaking cycles. On free-threaded builds, `tp_is_gc` is disabled entirely. The developer's own TODO asks if this causes leaks on GIL builds too. No clear resolution — collecting active greenlets would require unsafe finalization.

### Policy Decisions (POLICY)

- **Single-phase init**: Keep — greenlet's architecture requires process-global thread state
- **Limited API**: Not feasible — greenlet must access `PyThreadState` internals by design
- **7 dead compat code blocks**: Remove Python 2 / pre-3.9 shims for clarity
- **`GREENLET_PY310` macro**: Always true, can be simplified

## Strengths

- **Excellent RAII architecture**: `OwnedObject`/`BorrowedObject`/`Require()` wrappers prevent most reference counting bugs. Zero `goto`-based cleanup patterns
- **Clean PyErr_Clear usage**: Only 2 calls, both correct `WriteUnraisable` + `Clear` in destructor context
- **Advanced free-threading support**: Extensive `#ifdef Py_GIL_DISABLED` adaptation for allocator, counters, stack switching, GC. Significant effort already invested
- **Well-structured version guards**: `GREENLET_PY310`-`GREENLET_PY315` macros cleanly handle cross-version differences
- **Comprehensive exception handling**: C++ exceptions translate cleanly to Python exceptions via `PyErrOccurred` wrapper
- **Resource lifecycle**: RAII guards for GC state, mutex locks, tracing, parent pointers. Near-zero raw resource leaks

## Recommended Action Plan

### Immediate (FIX items)
1. **Fix `PyAddObject` error handler** — change `Py_DECREF(p)` to `Py_DECREF(new_object)` at `greenlet_refs.hpp:918`, or replace with `PyModule_AddObjectRef`
2. **Fix `delete_later` double-decref** — replace `Py_CLEAR(this->delete_later)` with `this->delete_later = nullptr` at `TPythonState.cpp:261`
3. **Add `c_stack_refs`/`stackpointer` to `set_initial_state`** for `GREENLET_PY314` + `Py_GIL_DISABLED` at `TPythonState.cpp:286-304`
4. **Fix `c_recursion_depth`** — use `Py_C_RECURSION_LIMIT - tstate->c_recursion_remaining` at `TPythonState.cpp:298`
5. **Wrap `PyLong_FromSsize_t` in `Require()`** at `greenlet.cpp:232`

### Short-term (CONSIDER items)
1. Add mutex for `deleteme` vector on free-threaded builds
2. Add `#ifndef Py_GIL_DISABLED` guard to `PyGreenletUnswitchable_Type.tp_is_gc`
3. Add `Py_VISIT(this->delete_later)` to `PythonState::tp_traverse` for 3.13+
4. Replace `PyModule_AddObject` with `PyModule_AddObjectRef` across the board
5. Clean up 7 dead compat code blocks

### Longer-term (POLICY)
1. Migrate dealloc to PEP 442 `tp_finalizer` (eliminate resurrection pattern)
2. Migrate `PyErr_Fetch`/`Restore` to `PyErr_GetRaisedException`/`SetRaisedException`
3. Investigate `tp_is_gc` leak potential on GIL builds (developer's own TODO)