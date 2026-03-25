# multidict C Extension Analysis Report

## Executive Summary

multidict is a compact (~6.5K lines), hand-written C extension implementing `MultiDict`, `CIMultiDict`, and `MultiDictProxy` — core data structures for aiohttp. It uses **modern best practices** (multi-phase init, module state, heap types via `PyType_FromSpec`, `pythoncapi_compat.h`, subinterpreter + free-threading declarations). Despite this, the analysis uncovered **significant bugs concentrated in the hash table implementation** (`hashtable.h`), which was completely rewritten in June 2025 and has had 7 fix commits since.

The most critical finding is a **double-free/use-after-free in `md_contains`** and a **crash from swapped format string arguments** in `_err_cannot_fetch`. The iterator and view types are also missing the heap type lifecycle management (`Py_VISIT(Py_TYPE(self))`) that the main dict types correctly implement.

**Total confirmed findings: 11 FIX, 3 CONSIDER.**

---

**Project:** multidict — Multi-value dictionary for HTTP headers
**Source:** `~/projects/laruche/repositories/multidict/multidict`
**Stats:** 9 C/H files (excl. pythoncapi_compat.h), ~6.5K lines, 62 functions, 11 heap types
**Architecture:** Multi-phase init, module state, heap types, `pythoncapi_compat.h`

---

## Critical Findings (FIX)

### Memory safety

| # | File | Bug | Agents |
|---|------|-----|--------|
| 1 | `hashtable.h:805-826` | **Double-free of `identity`** in `md_contains` — DECREF'd on success path, then XDECREF'd again at `fail` label when `_md_ensure_key` fails | git-history |
| 2 | `hashtable.h:1467-1475` | **Swapped format args** in `_err_cannot_fetch` — `%zd` gets a `char*`, `%s` gets an integer → **crash** (segfault on dereference) | git-history |

### Reference / resource leaks

| # | File | Bug | Agents |
|---|------|-----|--------|
| 3 | `hashtable.h:635-664` | Leaked `identity` in `md_next` — `Py_NewRef` stored in `*pidentity`, then overwritten with NULL in cleanup without DECREF | refcount, git-history |
| 4 | `hashtable.h:1791-1794` | Leaked `PyUnicodeWriter` + `key`/`value` in `md_repr` — `return NULL` instead of `goto fail` on concurrent modification | refcount, error-path, git-history |
| 5 | `hashtable.h:226-228` | Leaked `newkeys` in `_md_resize` — `htkeys_build_indices` failure doesn't free newly allocated key table | refcount, error-path |
| 6 | `hashtable.h:441-488` | Leaked `identity`/`key`/`value` in `_md_add_with_hash`/`_md_add_for_upd` — INCREF'd then steal-function fails on resize | refcount |
| 7 | `views.h:564-576, 673-685` | Leaked `tpl` in `itemsview_or2`/`itemsview_sub1` — `PyTuple_Pack` result never DECREF'd (×2 functions) | refcount, error-path |
| 8 | `views.h:901-908` | Leaked `key` in `itemsview_contains` — second `PySequence_GetItem` fails, key not released | refcount, error-path |

### Heap type lifecycle

| # | File | Bug | Agents |
|---|------|-----|--------|
| 9 | `iter.h:134-147` | Iterator types (×3) missing `Py_VISIT(Py_TYPE(self))` in traverse AND `Py_DECREF(tp)` in dealloc — type objects leaked | type-slot, refcount |
| 10 | `views.h:30-44` | View types (×3) missing `Py_VISIT(Py_TYPE(self))` in traverse AND `Py_DECREF(tp)` in dealloc — type objects leaked | type-slot, refcount |
| 11 | `istr.h:22-27` | `istr` type (non-GC) missing `Py_DECREF(Py_TYPE(self))` in dealloc — type object leaked | type-slot |

---

## Important Findings (CONSIDER)

| # | Finding | File | Source |
|---|---------|------|--------|
| 1 | `PyErr_Clear` in `_multidict_extend` swallows non-AttributeError exceptions | `_multidict.c:102-106` | error-path |
| 2 | `PyErr_Clear` in `multidict_tp_richcompare` swallows non-AttributeError | `_multidict.c:468-474` | error-path |
| 3 | `md_traverse` skips `entry->identity` (safe — identity is always `PyUnicode_Type`) | `hashtable.h:1858-1875` | type-slot |

---

## Strengths (What multidict does RIGHT)

This extension is a **model for modern C extension design** in several ways:

1. **Multi-phase init** with `Py_mod_exec` — correct subinterpreter support
2. **Complete module state** — all 14 `PyObject*` members in `mod_state` visited by `module_traverse`, cleared by `module_clear`, freed by `module_free`
3. **Heap types via `PyType_FromModuleAndSpec`** — all 11 types are proper heap types
4. **`pythoncapi_compat.h`** for backward compatibility
5. **`Py_MOD_PER_INTERPRETER_GIL_SUPPORTED` + `Py_MOD_GIL_NOT_USED`** declarations
6. **Main dict types correctly use `Py_VISIT(Py_TYPE(self))` in traverse** — the modern heap type idiom
7. **All `PyType_Slot` arrays properly terminated** with `{0, NULL}`
8. **Low complexity** — avg cyclomatic 3.4, no hotspots, max nesting 3

The bugs are in the **details** (iterator/view types missing what the main types have, error-path cleanup gaps) rather than the **architecture**.

---

## Logic Bug (Missed Optimization)

**`multidict_tp_init` passes `args` (tuple) instead of `arg` (first element) to `_multidict_clone_fast`** — the fast-clone path never triggers because a tuple never passes `AnyMultiDict_Check`. This is not a correctness bug but a missed performance optimization for `MultiDict(existing_multidict)`.

*Found by: error-path-analyzer*

---

## Priority Recommendations

### Immediate
1. **Finding 2 (swapped format args)** — swap `name` and `i` arguments to `PyErr_Format`. One-line fix, prevents crash.
2. **Finding 1 (double-free in `md_contains`)** — set `identity = NULL` after the `Py_DECREF` at line 807. One-line fix.
3. **Finding 4 (md_repr writer leak)** — change `return NULL` to `goto fail`. One-word change.

### Short-term
4. **Findings 9-11 (heap type lifecycle)** — add `Py_VISIT(Py_TYPE(self))` to iterator and view traverse functions; add `Py_DECREF(tp)` pattern to `istr_dealloc`. ~10 lines total.
5. **Findings 5-8 (error-path leaks)** — add cleanup calls on error paths in `_md_resize`, `_md_add_*`, view set operations, and `itemsview_contains`. ~15 lines total.

### Ongoing
6. Fix the `_multidict_clone_fast` optimization (`args` → `arg`)
7. Tighten `PyErr_Clear` to check `PyErr_ExceptionMatches` first



# multidict Report — Appendix: Reproducers

## Reproducer 1: `_err_cannot_fetch` swapped format args — Abort (Finding 2)

**Severity:** CRITICAL — user-triggerable interpreter abort from pure Python
**Confirmed on:** multidict 6.7.1, Python 3.14

The format string expects `%zd` (integer) then `%s` (string), but the arguments are passed in reverse order. The `%s` tries to dereference an integer as a pointer, triggering a CPython assertion failure.

```python
"""Reproducer: multidict _err_cannot_fetch swapped format args — abort.

hashtable.h:1467-1475 passes (name, i) but format expects (i, name).
  %zd receives a char* → prints garbage number
  %s receives an int → dereferences integer as pointer → crash

No special setup required.
Tested: multidict 6.7.1, Python 3.14.
"""
from multidict import MultiDict

class BadItem:
    """Looks like a 2-element sequence but __getitem__ raises."""
    def __len__(self):
        return 2
    def __getitem__(self, i):
        raise RuntimeError("intentional getitem failure")

MultiDict([BadItem()])
# Expected: ValueError with proper error message
# Actual: Aborted (core dumped)
#   Assertion `!_PyErr_Occurred(tstate)' failed.
```

**Output:**
```
python3: Python/generated_cases.c.h:12518: Assertion `!_PyErr_Occurred(tstate)' failed.
Aborted (core dumped)
```

---

## Reproducer 2: `md_repr` writer leak on concurrent modification — Memory leak (Finding 4)

**Severity:** MEDIUM — measurable memory leak
**Confirmed on:** multidict 6.7.1, Python 3.14

When a value's `__repr__` modifies the dict during `repr(md)`, the `PyUnicodeWriter` and any held `key`/`value` references are leaked.

```python
"""Reproducer: multidict md_repr leaks PyUnicodeWriter on concurrent modification.

hashtable.h:1791-1794 does 'return NULL' instead of 'goto fail',
leaking the writer allocated at line 1775.

Tested: multidict 6.7.1, Python 3.14.
"""
from multidict import MultiDict
import tracemalloc

class EvilValue:
    def __init__(self, md):
        self.md = md
    def __repr__(self):
        self.md.add("injected", "value")  # triggers version change
        return "evil"

tracemalloc.start()
snap1 = tracemalloc.take_snapshot()

for i in range(200):
    md = MultiDict()
    md.add("k", "v")
    md.add("evil", EvilValue(md))
    try:
        repr(md)
    except RuntimeError:
        pass  # "MultiDict changed during iteration"

snap2 = tracemalloc.take_snapshot()
stats = snap2.compare_to(snap1, 'lineno')
leaked = sum(s.size_diff for s in stats if s.size_diff > 0)
print(f"Memory leaked after 200 repr attempts: {leaked / 1024:.1f} KB")
# Expected: ~0 KB
# Actual: ~294 KB (leaked PyUnicodeWriter buffers)
```

**Output:**
```
Memory leaked after 200 repr attempts: 294.5 KB
```

---

## Reproducer 3: View set operations leak tuples — Memory leak (Finding 7)

**Severity:** HIGH — significant memory leak on common operations
**Confirmed on:** multidict 6.7.1, Python 3.14

`itemsview_or2` and `itemsview_sub1` create `PyTuple_Pack` tuples inside a loop that are never DECREF'd — one tuple leaked per entry per operation.

```python
"""Reproducer: multidict view set operations leak tuples.

views.h:564 (or2) and views.h:673 (sub1) create PyTuple_Pack results
that are never Py_DECREF'd. Leaks one tuple per entry per operation.

Tested: multidict 6.7.1, Python 3.14.
"""
from multidict import MultiDict
import tracemalloc

tracemalloc.start()
snap1 = tracemalloc.take_snapshot()

for i in range(500):
    md_a = MultiDict([(f"key{j}", f"val{j}") for j in range(20)])
    md_b = MultiDict([(f"key{j}", f"val{j}") for j in range(10, 30)])
    _ = md_a.items() | md_b.items()   # leaks tuples in itemsview_or2
    _ = md_a.items() - md_b.items()   # leaks tuples in itemsview_sub1

snap2 = tracemalloc.take_snapshot()
stats = snap2.compare_to(snap1, 'lineno')
leaked = sum(s.size_diff for s in stats if s.size_diff > 0)
print(f"Memory leaked after 500 view set ops: {leaked / 1024:.1f} KB")
# Expected: ~0 KB
# Actual: ~2400 KB (one tuple per entry per operation)
```

**Output:**
```
Memory leaked after 500 view set ops: 2418.7 KB
```

---

## Reproducer 4: Iterator/View/istr type refcount leak — Type object leak (Findings 9-11)

**Severity:** MEDIUM — type refcount grows unboundedly
**Confirmed on:** multidict 6.7.1, Python 3.14

Iterator, view, and istr types are heap types but their dealloc/traverse functions don't manage the type reference. Each create/destroy cycle leaks exactly 1 reference to the type object.

```python
"""Reproducer: multidict iterator/view/istr types leak type references.

iter.h:134 — multidict_iter_dealloc: no Py_DECREF(Py_TYPE(self))
views.h:30 — multidict_view_dealloc: no Py_DECREF(Py_TYPE(self))
istr.h:22 — istr_dealloc: no Py_DECREF(Py_TYPE(self))
traverse functions also missing Py_VISIT(Py_TYPE(self)).

Tested: multidict 6.7.1, Python 3.14.
"""
from multidict import MultiDict, istr
import sys

md = MultiDict([("a", "1"), ("b", "2")])

# Test iterator type leak
it = iter(md.keys())
iter_type = type(it)
del it
baseline = sys.getrefcount(iter_type)
for i in range(1000):
    it = iter(md.keys())
    list(it)
    del it
after = sys.getrefcount(iter_type)
print(f"KeysIter type: baseline={baseline}, after={after}, leaked={after-baseline}")

# Test view type leak
view_type = type(md.keys())
baseline = sys.getrefcount(view_type)
for i in range(1000):
    v = md.keys()
    del v
after = sys.getrefcount(view_type)
print(f"KeysView type: baseline={baseline}, after={after}, leaked={after-baseline}")

# Test istr type leak
baseline = sys.getrefcount(istr)
for i in range(1000):
    s = istr("hello")
    del s
after = sys.getrefcount(istr)
print(f"istr type:     baseline={baseline}, after={after}, leaked={after-baseline}")

# Expected: 0 leaked for all
# Actual: exactly 1000 leaked for each — one per object lifecycle
```

**Output:**
```
KeysIter type: baseline=8, after=1008, leaked=1000
KeysView type: baseline=1029, after=2029, leaked=1000
istr type:     baseline=10, after=1010, leaked=1000
```

---

## Summary Table

| # | Finding | Reproducer | Result |
|---|---------|-----------|--------|
| 2 | Swapped format args in `_err_cannot_fetch` | **ABORT CONFIRMED** | Assertion failure, core dump |
| 4 | `md_repr` writer leak | **LEAK CONFIRMED** | 294.5 KB from 200 attempts |
| 7 | View set ops tuple leak | **LEAK CONFIRMED** | 2418.7 KB from 500 operations |
| 9-11 | Iterator/view/istr type refcount leak | **LEAK CONFIRMED** | Exactly 1000 refs leaked per 1000 cycles |
| 1 | Double-free in `md_contains` | Code-confirmed | Requires `_md_ensure_key` failure (OOM) |
| 3 | Identity leak in `md_next` | Code-confirmed | Latent — current callers don't trigger path |
| 5-6 | Resize / add error-path leaks | Code-confirmed | Require OOM during resize |
| 8 | Key leak in `itemsview_contains` | Code-confirmed | Requires `PySequence_GetItem` failure |

**1 confirmed crash reproducer**, **3 confirmed memory leak reproducers** (all measurable from pure Python), **5 code-confirmed bugs** requiring OOM or unusual failure conditions.