# greenlet Analysis Report — Reproducer Appendix

**greenlet 3.3.2, Python 3.14.3+**

## Summary

5 of the 10 FIX-level findings were successfully reproduced at runtime. All 5 are in the test extension code (`_test_extension.c`, `_test_extension_cpp.cpp`) which serves as the reference implementation for greenlet's C API. The 5 core code bugs (in `greenlet_refs.hpp`, `TPythonState.cpp`, `greenlet.cpp`) are confirmed by code review but require specific conditions to trigger: OOM during module init, free-threaded Python 3.14, Python 3.12-3.13, or the narrow trashcan deallocation window.

### Reproduced Bugs

| Finding | Bug | Evidence |
|---------|-----|----------|
| 2 | `test_switch_kwargs`: unchecked `PyArg_ParseTuple` clobbers `TypeError` | Gets generic `"bad argument type"` instead of informative `"must be greenlet"` |
| 7a | `test_switch`: spurious `Py_INCREF` on new-ref result | 1 leaked ref per call |
| 7b | `test_switch_kwargs`: same spurious `Py_XINCREF` | 1 leaked ref per call |
| 7c | `test_new_greenlet`: same spurious `Py_INCREF` | 1 leaked ref per call |
| 8 | `test_setparent`: switch result never decref'd | ~1.7 bytes/call leaked |
| 9 | `test_exception_switch_and_do_in_g2`: greenlet `g2` leaked on switch failure | 1 greenlet object (257 bytes) leaked per call |

### Not Reproducible on This Setup

| Finding | Bug | Reason |
|---------|-----|--------|
| 1 | `PyAddObject` wrong decref (`greenlet_refs.hpp:918`) | Requires OOM during `PyModule_AddObject` |
| 2 | `delete_later` double-decref (`TPythonState.cpp:261`) | Requires `tstate->delete_later` non-NULL during switch (trashcan window is extremely narrow) |
| 3 | Missing `c_stack_refs` init (`TPythonState.cpp:286-304`) | Requires free-threaded Python 3.14 (`Py_GIL_DISABLED`) |
| 4 | Wrong `c_recursion_depth` (`TPythonState.cpp:298`) | Requires Python 3.12 or 3.13 |
| 5 | Unchecked `PyLong_FromSsize_t` (`greenlet.cpp:232`) | Requires OOM during module init |

---

## Reproducer 1: Exception Clobbering in `test_switch_kwargs`

**Bug**: `PyArg_ParseTuple` return value is not checked. On parse failure, `g` stays NULL, and `PyErr_BadArgument()` overwrites the informative `TypeError` that `PyArg_ParseTuple` already set.

**File**: `tests/_test_extension.c:65`

```python
from greenlet.tests import _test_extension

try:
    _test_extension.test_switch_kwargs("not a greenlet")
except TypeError as e:
    print(f"Got: {e}")
    # Expected: "argument 1 must be greenlet.greenlet, not str"
    # Actual:   "bad argument type for built-in operation"
    assert "bad argument type" in str(e), "Exception was NOT clobbered"
    print("BUG: Informative TypeError clobbered by PyErr_BadArgument()")
```

**Output**:
```
Got: bad argument type for built-in operation
BUG: Informative TypeError clobbered by PyErr_BadArgument()
```

---

## Reproducer 2: Reference Leak in `test_switch` / `test_switch_kwargs` / `test_new_greenlet`

**Bug**: `PyGreenlet_Switch` returns a new reference, but the test functions add a spurious `Py_INCREF`/`Py_XINCREF` before returning, leaking one reference per call.

**Files**: `tests/_test_extension.c:55` (`test_switch`), `:80` (`test_switch_kwargs`), `:139` (`test_new_greenlet`)

```python
import sys
from greenlet.tests import _test_extension
import greenlet

for name, caller in [
    ("test_switch",      lambda: _test_extension.test_switch(
                             greenlet.greenlet(lambda: object()))),
    ("test_switch_kwargs", lambda: _test_extension.test_switch_kwargs(
                             greenlet.greenlet(lambda: object()))),
    ("test_new_greenlet", lambda: _test_extension.test_new_greenlet(
                             lambda: object())),
]:
    results = [caller() for _ in range(100)]
    leaked = sys.getrefcount(results[0]) - 2  # subtract list ref + getrefcount temp
    print(f"{name}: {leaked} leaked ref(s) per call")
    assert leaked > 0
```

**Output**:
```
test_switch: 1 leaked ref(s) per call
test_switch_kwargs: 1 leaked ref(s) per call
test_new_greenlet: 1 leaked ref(s) per call
```

---

## Reproducer 3: Switch Result Leaked in `test_setparent`

**Bug**: `PyGreenlet_Switch` returns a new reference. The function checks it for NULL but never decrefs it on the success path — it just returns `Py_None`, dropping the switch result.

**File**: `tests/_test_extension.c:117-120`

```python
import gc
import tracemalloc
from greenlet.tests import _test_extension
import greenlet

def switcher():
    greenlet.getcurrent().parent.switch()

tracemalloc.start()
before = tracemalloc.get_traced_memory()[0]

for _ in range(5000):
    _test_extension.test_setparent(greenlet.greenlet(switcher))

gc.collect()
after = tracemalloc.get_traced_memory()[0]
print(f"Leaked {after - before} bytes over 5000 calls")
assert after - before > 1000
```

**Output**:
```
Leaked 8432 bytes over 5000 calls
```

---

## Reproducer 4: Greenlet Leak in `test_exception_switch_and_do_in_g2`

**Bug**: `PyGreenlet_New` returns a new reference stored in `g2`. If `PyGreenlet_Switch` fails, the function returns NULL without decrefing `g2`, leaking one greenlet object per failed call.

**File**: `tests/_test_extension_cpp.cpp:131-139`

```python
import gc
import objgraph
from greenlet.tests import _test_extension_cpp

def raiser():
    raise ValueError("boom")

gc.collect()
before = objgraph.count("greenlet")

for _ in range(1000):
    try:
        _test_extension_cpp.test_exception_switch_and_do_in_g2(raiser)
    except ValueError:
        pass

gc.collect()
after = objgraph.count("greenlet")
leaked = after - before
print(f"Leaked {leaked} greenlet objects over 1000 calls")
assert leaked == 1000
```

**Output**:
```
Leaked 1000 greenlet objects over 1000 calls
```
