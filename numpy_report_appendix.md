# NumPy C Extension Analysis — Reproducer Appendix

## Confirmed Reproducers

5 crashes confirmed with pure Python reproducers. 3 require no special setup (pure Python input), 2 use `_testcapi.set_nomemory` for OOM triggering.

All tested with NumPy 2.4.3 on Python 3.14.

---

### Reproducer 1: `arr.tofile()` segfault on non-ASCII format string

**Bug:** `convert.c:301` — `PyUnicode_AsASCIIString()` returns NULL on non-ASCII, result passed to `PyBytes_GET_SIZE(NULL)` and `PyBytes_AS_STRING(NULL)` — with the GIL released.

**Severity:** Critical — user-triggerable from pure Python, crashes with GIL released (potential interpreter corruption).

```python
"""Reproducer: PyArray_ToFile segfault on non-ASCII format string.

PyUnicode_AsASCIIString returns NULL for non-ASCII characters.
The NULL is passed to PyBytes_GET_SIZE/PyBytes_AS_STRING macros
which dereference it, causing a segfault. The GIL is released
at the time of crash (NPY_BEGIN_ALLOW_THREADS), making this
especially dangerous.

No special setup required — pure Python input triggers the crash.
Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np
import tempfile

arr = np.array([1.0, 2.0, 3.0])
with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
    fname = f.name

# This segfaults — the non-ASCII 'é' causes PyUnicode_AsASCIIString to fail
arr.tofile(fname, sep=",", format="%sé")
# Expected: UnicodeEncodeError or ValueError
# Actual: Segmentation fault
```

**Output:**
```
Fatal Python error: Segmentation fault
  File "<string>", line 9 in <module>
  _multiarray_umath.cpython-314-x86_64-linux-gnu.so, at +0x18d639
```

---

### Reproducer 2: `arr.flags[]` segfault on non-ASCII key

**Bug:** `flagsobject.c:588` — Same `PyUnicode_AsASCIIString` → NULL pattern. `PyBytes_AS_STRING(NULL)` and `PyBytes_GET_SIZE(NULL)` crash.

**Severity:** Critical — user-triggerable from pure Python.

```python
"""Reproducer: arrayflags_setitem segfault on non-ASCII key.

PyUnicode_AsASCIIString returns NULL for non-ASCII characters.
The NULL is passed to PyBytes_AS_STRING/PyBytes_GET_SIZE which
dereference it, causing a segfault.

No special setup required.
Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np

arr = np.array([1, 2, 3])

# This segfaults — the non-ASCII 'é' causes PyUnicode_AsASCIIString to fail
arr.flags["é"] = True
# Expected: KeyError or ValueError
# Actual: Segmentation fault
```

**Output:**
```
Fatal Python error: Segmentation fault
  File "<string>", line 7 in <module>
  _multiarray_umath.cpython-314-x86_64-linux-gnu.so, at +0x1ccf1e
```

---

### Reproducer 3: `nditer.multi_index` segfault with raising sequence

**Bug:** `nditer_pywrap.c:1678` — `PySequence_GetItem()` returns NULL when `__getitem__` raises. The NULL is passed to `PyLong_AsLong(NULL)` and then `Py_DECREF(NULL)`.

**Severity:** Critical — user-triggerable from pure Python with a custom sequence.

```python
"""Reproducer: npyiter_multi_index_set segfault with raising sequence.

PySequence_GetItem returns NULL when __getitem__ raises.
The NULL is passed to PyLong_AsLong and Py_DECREF, both of
which dereference it.

No special setup required — any sequence whose __getitem__ raises.
Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np


class BadSequence:
    def __len__(self):
        return 2

    def __getitem__(self, i):
        if i == 1:
            raise RuntimeError("intentional error")
        return 0


arr = np.zeros((3, 4))
it = np.nditer(arr, flags=["multi_index"])

# This segfaults — __getitem__ raises, PySequence_GetItem returns NULL,
# then PyLong_AsLong(NULL) and Py_DECREF(NULL) crash
it.multi_index = BadSequence()
# Expected: RuntimeError("intentional error")
# Actual: Segmentation fault
```

**Output:**
```
Fatal Python error: Segmentation fault
  File "<string>", line 19 in <module>
  _multiarray_umath.cpython-314-x86_64-linux-gnu.so, at +0x20e295
```

---

### Reproducer 4: `pickle.dumps(arr)` abort under OOM

**Bug:** `methods.c:1798-1832` — Multiple unchecked `PyObject_GetAttrString`/`Py_BuildValue` results stored into tuple via `PyTuple_SET_ITEM`. Under OOM, NULL is stored in the tuple, causing CPython's "function returned result with exception set" assertion.

**Severity:** Medium — requires OOM (via `_testcapi.set_nomemory`).

```python
"""Reproducer: ndarray pickle abort under OOM.

array_reduce stores unchecked API results into a tuple via
PyTuple_SET_ITEM. Under OOM, NULL is stored, causing CPython
to abort with "a function returned a result with an exception set".

Requires: _testcapi module (CPython debug/test build).
Tested: NumPy 2.4.3, Python 3.14.
"""
import _testcapi
import pickle

import numpy as np

arr = np.array([1, 2, 3])

for n in range(1, 500):
    _testcapi.set_nomemory(n, 0)
    try:
        data = pickle.dumps(arr)
        _testcapi.remove_mem_hooks()
    except MemoryError:
        _testcapi.remove_mem_hooks()
    except Exception:
        _testcapi.remove_mem_hooks()
# Aborts with:
# Fatal Python error: _Py_CheckFunctionResult:
#   a function returned a result with an exception set
```

**Output:**
```
Fatal Python error: _Py_CheckFunctionResult: a function returned a result with an exception set
Python runtime state: initialized
object type name: MemoryError
```

---

### Reproducer 5: `nditer.copy()` segfault under OOM

**Bug:** `nditer_constr.c:540` — `PyObject_Malloc(size)` returns NULL, result passed directly to `memcpy(NULL, iter, size)` — writes to address 0.

**Severity:** Medium — requires OOM (via `_testcapi.set_nomemory`).

```python
"""Reproducer: NpyIter_Copy segfault under OOM.

PyObject_Malloc returns NULL, then memcpy(NULL, iter, size)
writes to address 0, causing an immediate segfault.

Requires: _testcapi module (CPython debug/test build).
Tested: NumPy 2.4.3, Python 3.14.
"""
import _testcapi
import numpy as np

arr = np.zeros((10, 10))
it = np.nditer(arr, flags=["multi_index"])

for n in range(1, 500):
    _testcapi.set_nomemory(n, 0)
    try:
        it2 = it.copy()
        _testcapi.remove_mem_hooks()
    except MemoryError:
        _testcapi.remove_mem_hooks()
    except Exception:
        _testcapi.remove_mem_hooks()
# Segfaults before completing the loop
```

**Output:**
```
Fatal Python error: Segmentation fault
  _multiarray_umath.cpython-314-x86_64-linux-gnu.so, at +0x209f46
  libc.so.6, at +0x1b1c54  (memcpy)
```

---

## Summary Table

| # | Bug | Reproducer | Trigger | Crash Type |
|---|-----|-----------|---------|------------|
| 1 | `tofile()` non-ASCII format | Pure Python | Any non-ASCII char in format | **SEGFAULT** (GIL released) |
| 2 | `arr.flags[]` non-ASCII key | Pure Python | Any non-ASCII char as key | **SEGFAULT** |
| 3 | `nditer.multi_index =` bad seq | Pure Python | Sequence with raising `__getitem__` | **SEGFAULT** |
| 4 | `pickle.dumps(arr)` OOM | `_testcapi` | OOM during pickle | **ABORT** (assertion) |
| 5 | `nditer.copy()` OOM | `_testcapi` | OOM during iterator copy | **SEGFAULT** (memcpy to NULL) |

## Bugs Not Reproduced (attempted)

| Bug | Why Not Reproduced |
|-----|-------------------|
| `fill_zero_object_strided_loop` OOM | `PyLong_FromLong(0)` uses small int cache, never allocates |
| `PyArray_Arange` OOM | `PyFloat_FromDouble` allocation hard to target with set_nomemory |
| `dtype_transfer.c` clone OOM | `PyMem_Malloc` uses raw allocator, not pymalloc hooks |
| `buffer.c` format OOM | Subarray path allocation hard to target |
| `OBJECT_dot` empty vector | Fixed in NumPy 2.4.3 (the installed version) |

## Bugs Not Attempted (code-reading-only)

These bugs were confirmed by reading the source but require specific conditions that are difficult to set up:

| Bug | Requirement |
|-----|------------|
| 3 use-after-free in `dispatching.cpp` | Requires specific ufunc dispatch path with borrowed ref invalidation |
| `Py_DECREF(cast_impl)` on borrowed ref (`usertypes.c:364`) | Requires user-registered cast to trigger the code path |
| Borrowed refs across `_PyArray_SetNumericOps` (`umathmodule.c:223`) | Requires OOM during module init that triggers dict resize |
| 2 timsort heap corruption (`timsort.cpp:79,1821`) | Requires realloc failure during sort of very large array |
| `sfloat_get_ufunc` NULL deref | Requires missing ufunc attribute in scaled float test dtype |

## Notes for NumPy Maintainers

1. **Findings 1 and 2 are the highest priority** — they are pure Python segfaults triggered by ordinary non-ASCII input, with no special setup needed. Finding 1 is especially dangerous because the crash occurs with the GIL released.

2. **Finding 3 is also high priority** — any custom sequence class whose `__getitem__` can raise will crash `nditer.multi_index` assignment. This is a user-facing API.

3. **The `_testcapi.set_nomemory` technique** is effective for finding OOM crash paths. We recommend NumPy's own test suite incorporate similar OOM-injection tests for critical allocation paths.

4. **Scanner limitation discovered**: `NPY_NO_EXPORT` macro causes tree-sitter to misparse type definitions, hiding all 14 of NumPy's type definitions from our `scan_type_slots.py` scanner. This is filed as a cext-review-toolkit improvement.
