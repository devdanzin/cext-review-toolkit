# C Extension Bug Reproducer Techniques

A catalog of Python-level techniques for triggering bugs found by code review in C extensions. Each technique exploits a specific Python/C API contract violation from pure Python, without needing to modify the extension source.

## Prerequisites

The extension must be **installed and importable**. Building extensions is project-specific and cannot be generalized.

## Useful tools

- `sys.getrefcount(obj)` — check for reference leaks (expected: N+1 where N is real refs, +1 for the getrefcount arg)
- `tracemalloc` — measure memory growth from leaks
- `gc.collect()` + `gc.get_objects()` — detect uncollectable cycles
- `objgraph.count(typename)` — count live objects of a type
- `weakref.ref(obj)` — verify an object is actually freed
- `sys.gettotalrefcount()` — total refcount delta (debug builds only)
- `id()` on freed objects — use-after-free detection (may segfault or return stale data)

---

## Technique 1: Metaclass hiding attributes

**Triggers**: Unchecked `PyObject_GetAttrString(Py_TYPE(obj), "__name__")` and similar.

**Bug class**: NULL from `PyObject_GetAttrString` flows into `strcmp`, `PyUnicode_AsUTF8`, or `Py_DECREF` without a check.

**How it works**: A metaclass can intercept attribute lookup before it reaches C-level descriptors like `type.__name__`.

```python
class HiddenNameMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            raise AttributeError("no __name__")
        return super().__getattribute__(name)

class Victim(metaclass=HiddenNameMeta):
    pass  # Py_TYPE(obj).__name__ lookup will fail
```

Can also hide `__module__`, `__qualname__`, `__bases__`, `__mro__`, etc.

**Confirmed on**: protobuf (`convert.c` numpy detection — segfault)

---

## Technique 2: Stateful `__hash__`

**Triggers**: Unguarded `PyErr_Clear()` after dict operations, or assumptions that `__hash__` is pure.

**Bug class**: Code does a dict lookup, clears the error, and continues — but the error was `MemoryError` from `__hash__`, not `KeyError`.

```python
class StatefulHash:
    def __init__(self):
        self._call_count = 0
    def __hash__(self):
        self._call_count += 1
        if self._call_count > 1:
            raise MemoryError("injected OOM on second hash")
        return 42
    def __eq__(self, other):
        return self is other
```

**Useful for**: Testing dict operations where the first lookup succeeds (to insert) but a second fails.

---

## Technique 3: `__eq__` that raises

**Triggers**: RichCompare implementations that store the result of `IsEqual()` in a `bool` (truncating -1 to true), or comparison code that doesn't check for errors.

**Bug class**: Three-state return (-1/0/1) truncated to bool, or exception from `__eq__` not propagated.

```python
class BadEq:
    def __eq__(self, other):
        raise RuntimeError("comparison error")
    def __ne__(self, other):
        raise RuntimeError("comparison error")
    def __hash__(self):
        return 0
    def __iter__(self):
        return iter([])
    def __len__(self):
        return 0
```

Add `__iter__` and `__len__` to pass any "is this a sequence/mapping" checks before the comparison is reached.

**Confirmed on**: protobuf (`descriptor_containers.c` RichCompare — error silently swallowed)

---

## Technique 4: Descriptor with `__get__` that raises

**Triggers**: Unguarded `PyErr_Clear()` after `type_getattro` or `PyObject_GetAttr` in attribute lookup fallback chains.

**Bug class**: `tp_getattro` fails with non-`AttributeError`, but the code clears unconditionally and falls through to a secondary lookup.

```python
class BombDescriptor:
    def __get__(self, obj, objtype=None):
        raise KeyboardInterrupt("should not be swallowed")

# Inject into a type's dict
SomeType.attr_name = BombDescriptor()
# Now SomeType.attr_name raises KeyboardInterrupt, which the C code may swallow
```

**Confirmed on**: protobuf (`message.c` MessageMeta_GetAttr — `KeyboardInterrupt` replaced with `AttributeError`)

---

## Technique 5: Builtin subclass with malicious methods

**Triggers**: Code that assumes `list.append()`, `dict.__getitem__()`, etc. behave normally.

**Bug class**: C code calls `PyList_Append` or similar on an object that *could* be a subclass with overridden methods, though `PyList_Append` itself uses the C slot. More useful for `PyObject_SetItem`, `PyObject_GetItem`, etc.

```python
class EvilList(list):
    def __setitem__(self, key, value):
        raise MemoryError("injected OOM in __setitem__")

class EvilDict(dict):
    def __setitem__(self, key, value):
        raise MemoryError("injected OOM in __setitem__")
    def __getitem__(self, key):
        raise KeyError("always missing")
    def __contains__(self, key):
        raise RuntimeError("injected error in __contains__")
```

**Useful for**: Testing code that accepts arbitrary mapping/sequence arguments and passes them to Python APIs that dispatch through `tp_as_mapping`/`tp_as_sequence`.

---

## Technique 6: `__del__` that triggers side effects

**Triggers**: Resource lifecycle bugs, reentrance during deallocation, GIL state issues.

**Bug class**: Object deallocation runs arbitrary Python code via `__del__`, which can trigger greenlet switches, modify shared state, or cause reentrant calls.

```python
class SwitchOnDel:
    def __init__(self, callback):
        self.callback = callback
    def __del__(self):
        self.callback()

# Example: switch greenlets during deallocation
import greenlet
def worker():
    greenlet.getcurrent().parent.switch()
g = greenlet.greenlet(worker)
g.switch()
obj = SwitchOnDel(lambda: g.switch())
del obj  # Triggers greenlet switch during dealloc
```

---

## Technique 7: `__index__` / `__int__` / `__float__` that raises

**Triggers**: Unguarded `PyErr_Clear()` after `PyNumber_Index()`, `PyLong_AsLong()`, etc.

**Bug class**: Numeric conversion clears error and falls back, but the error was `MemoryError` not `TypeError`.

```python
class BadIndex:
    _calls = 0
    def __index__(self):
        BadIndex._calls += 1
        if BadIndex._calls > 1:
            raise MemoryError("OOM on second __index__ call")
        return 42

class BadFloat:
    def __float__(self):
        raise MemoryError("OOM in __float__")
```

---

## Technique 8: Iterator that fails mid-iteration

**Triggers**: Unchecked `PyIter_Next()` return, missing cleanup on iteration failure, partial mutation bugs.

**Bug class**: C code iterates with a for loop but doesn't check for errors mid-loop, or doesn't clean up partial results on failure.

```python
class FailAfterN:
    def __init__(self, n, items):
        self.n = n
        self.items = items
        self.i = 0
    def __iter__(self):
        return self
    def __next__(self):
        if self.i >= self.n:
            raise RuntimeError("injected iteration failure")
        if self.i >= len(self.items):
            raise StopIteration
        val = self.items[self.i]
        self.i += 1
        return val
    def __len__(self):  # For code that pre-allocates based on len
        return len(self.items)
```

**Useful for**: `extend()`, `update()`, bulk insert operations. The `__len__` override triggers the pre-allocation path while the iterator fails partway.

---

## Technique 9: `__len__` that lies or raises

**Triggers**: Unguarded `PyObject_Size()` / `PyObject_Length()` followed by `PyErr_Clear()`.

**Bug class**: Code tries to get length for pre-allocation, clears the error if `__len__` fails, but the error was `MemoryError` not `TypeError`.

```python
class LyingLen:
    def __len__(self):
        return 1000000  # Causes massive over-allocation
    def __iter__(self):
        return iter([1, 2, 3])

class RaisingLen:
    def __len__(self):
        raise MemoryError("OOM in __len__")
    def __iter__(self):
        return iter([1, 2, 3])
```

**Confirmed on**: protobuf (`repeated.c` RepeatedContainer_Extend — `PyErr_Clear` after `PyObject_Size`)

---

## Technique 10: Out-of-range indices and boundary values

**Triggers**: Index clamping bugs, integer overflow, off-by-one errors.

**Bug class**: C code clamps indices to valid range instead of raising `IndexError`, or doesn't handle negative indices correctly.

```python
# Large positive index (may be clamped instead of rejected)
container.pop(999999999)
container[sys.maxsize]

# Large negative index
container[-999999999]

# Zero-length container edge cases
empty_container.pop()
empty_container.pop(0)
empty_container[-1]
```

**Confirmed on**: protobuf (`repeated.c` pop — clamps to last element instead of IndexError)

---

## Technique 11: Concurrent modification during callback

**Triggers**: Borrowed reference invalidation, iterator invalidation, dict mutation during iteration.

**Bug class**: C code holds a borrowed reference to a container element, then calls Python code that modifies the container, invalidating the reference.

```python
class MutatingCallback:
    def __init__(self, container):
        self.container = container
    def __eq__(self, other):
        self.container.clear()  # Mutate during comparison
        return NotImplemented
    def __hash__(self):
        return 0
```

**Useful for**: Testing dict/list operations where callbacks (`__eq__`, `__hash__`, `__del__`) can modify the container being operated on.

---

## Technique 12: Weakref callback that triggers during sensitive operations

**Triggers**: Use-after-free, double-free, reentrant deallocation.

```python
import weakref

def destroy_callback(ref):
    # This runs during garbage collection
    # Can trigger arbitrary Python code at unexpected times
    print("weakref callback fired")

obj = SomeExtensionType()
ref = weakref.ref(obj, destroy_callback)
del obj  # Callback fires during dealloc
```

---

## Technique 13: `__repr__` / `__str__` that raises or has side effects

**Triggers**: Unchecked `PyObject_Repr()` / `PyObject_Str()` in error messages, logging, assertions.

**Bug class**: C code calls `PyObject_Repr` to build an error message, doesn't check for NULL, and crashes or leaks.

```python
class BadRepr:
    def __repr__(self):
        raise MemoryError("OOM in __repr__")
    def __str__(self):
        raise RuntimeError("error in __str__")
```

---

## Technique 14: `__bool__` that raises

**Triggers**: Unchecked `PyObject_IsTrue()` calls.

**Bug class**: C code checks truthiness of a Python object without handling the error case.

```python
class BadBool:
    def __bool__(self):
        raise RuntimeError("error in __bool__")
```

---

## Technique 15: Buffer protocol abuse

**Triggers**: `PyObject_GetBuffer` without `PyBuffer_Release`, or buffer content mutation during use.

```python
class EvilBuffer:
    def __init__(self):
        self._data = bytearray(1024)
    def __buffer__(self, flags):
        # Return a buffer, then mutate the backing store
        import threading
        def mutate():
            import time; time.sleep(0.001)
            self._data[:] = b'\xff' * 1024
        threading.Thread(target=mutate).start()
        return memoryview(self._data)
```

---

## Technique 16: Measuring reference leaks

**Pattern for detecting per-call reference leaks in any operation.**

```python
import sys, gc

def measure_refleak(func, iterations=1000):
    """Call func() repeatedly and check for refcount growth."""
    # Warm up
    for _ in range(10):
        func()
    gc.collect()

    # Measure
    results = []
    for _ in range(iterations):
        obj = func()
        results.append(obj)

    sample = results[0]
    actual = sys.getrefcount(sample)
    expected = 2  # list ref + getrefcount temp
    leaked = actual - expected

    if leaked > 0:
        print(f"LEAK: {leaked} ref(s) leaked per call")

    del results
    gc.collect()
    return leaked

# Example usage:
# measure_refleak(lambda: ext.some_operation())
```

---

## Technique 17: Measuring memory leaks with tracemalloc

```python
import tracemalloc, gc

def measure_memleak(func, iterations=5000):
    """Call func() repeatedly and check for memory growth."""
    tracemalloc.start()
    gc.collect()
    before = tracemalloc.get_traced_memory()[0]

    for _ in range(iterations):
        func()

    gc.collect()
    after = tracemalloc.get_traced_memory()[0]
    delta = after - before
    per_call = delta / iterations

    if delta > iterations:  # More than 1 byte per call
        print(f"LEAK: {delta} bytes over {iterations} calls ({per_call:.1f} bytes/call)")

    tracemalloc.stop()
    return delta
```

---

## Technique 18: OOM injection via `_testcapi.set_nomemory`

**Triggers**: Any unchecked allocation (`PyType_GenericAlloc`, `PyList_New`, `PyDict_New`, `malloc`, etc.) that dereferences the result without a NULL check.

**Bug class**: C code calls an allocation function, doesn't check for NULL, and dereferences the result. Under normal conditions these rarely fail, but `_testcapi.set_nomemory(n, 0)` forces all allocations after the `n`-th one to fail, systematically exercising every OOM error path.

```python
import _testcapi

def oom_scan(func, max_n=500):
    """Call func() with OOM injected at every allocation point.

    If func() has an unchecked allocation, this will segfault
    at the specific allocation number that triggers it.
    """
    for n in range(1, max_n):
        _testcapi.set_nomemory(n, 0)
        try:
            func()
            _testcapi.remove_mem_hooks()
            break  # No more allocations to fail
        except MemoryError:
            _testcapi.remove_mem_hooks()
        except:
            _testcapi.remove_mem_hooks()
    print(f"Survived {max_n} iterations without segfault")

# Example: test trait creation under OOM
from traits.api import HasTraits, Int, on_trait_change

class Obj(HasTraits):
    x = Int()
    @on_trait_change("x")
    def _x_changed(self, new):
        pass

oom_scan(lambda: setattr(Obj(), "x", 42))
# Segmentation fault at whichever allocation lacks a NULL check
```

**How it works**: `_testcapi.set_nomemory(n, 0)` installs a custom memory allocator that lets the first `n` allocations succeed, then returns NULL for all subsequent ones. This systematically tests every allocation point in the code path. When an unchecked allocation fails, the NULL return is dereferenced, causing a segfault.

**Requirements**: CPython with `_testcapi` module (standard in CPython builds, not available in all distributions). Call `_testcapi.remove_mem_hooks()` in every exception handler to restore normal allocation before any cleanup code runs.

**Confirmed on**: traits (`get_trait` unchecked `PyType_GenericAlloc` — segfault)

---

## Applicability Matrix

| Technique | Triggers Bug Class | Needs Special Object | Difficulty |
|-----------|-------------------|---------------------|------------|
| 1. Hidden attributes | NULL deref in strcmp/API | Metaclass | Easy |
| 2. Stateful hash | Swallowed exceptions in dict ops | Custom class | Medium |
| 3. Raising __eq__ | RichCompare error truncation | Custom class | Easy |
| 4. Bomb descriptor | Swallowed exceptions in getattr | Descriptor + type dict access | Easy |
| 5. Evil subclasses | Unchecked container ops | Subclass of list/dict | Easy |
| 6. __del__ side effects | Reentrant dealloc, resource lifecycle | Custom class | Medium |
| 7. Raising __index__ | Swallowed numeric conversion errors | Custom class | Easy |
| 8. Failing iterator | Partial mutation, cleanup bugs | Custom iterator | Medium |
| 9. Lying __len__ | Over-allocation, swallowed size errors | Custom class | Easy |
| 10. Boundary indices | Clamping, overflow, off-by-one | Built-in values | Easy |
| 11. Mutating callback | Borrowed ref invalidation | Custom class | Hard |
| 12. Weakref callback | Use-after-free, reentrance | weakref | Medium |
| 13. Raising __repr__ | Unchecked PyObject_Repr | Custom class | Easy |
| 14. Raising __bool__ | Unchecked PyObject_IsTrue | Custom class | Easy |
| 15. Buffer abuse | Buffer lifecycle bugs | Custom buffer | Hard |
| 16. Refleak measurement | Reference count bugs | sys.getrefcount | Easy |
| 17. Memleak measurement | Memory leaks | tracemalloc | Easy |
| 18. OOM injection | Unchecked allocations, NULL deref on OOM | _testcapi | Easy |
