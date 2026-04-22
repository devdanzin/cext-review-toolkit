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
- `_testcapi.set_nomemory(n, stop)` — OOM injection at the Python allocator level (see Technique 18). Hooks `PYMEM_DOMAIN_RAW` only.
- `libfiu` + `fiu_posix_preload.so` — OOM injection at the system allocator level, including foreign C libraries linked by the extension (see Technique 23). Reaches `malloc`/`calloc`/`realloc`/`mmap`/`open`/etc. Complements `set_nomemory`.

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

### Technique 5b: File-like objects with malicious methods

A file-like object whose `read()`, `write()`, `seek()`, `fileno()`, or `__fspath__()` raises can expose error-path bugs in I/O code. The key is to control *when* the failure happens — first call vs. second call produces different behavior.

```python
class DelayedFailFile:
    """Succeeds on first read, fails on second — exposes mid-parse error handling."""
    def __init__(self, first_data, exception=MemoryError):
        self.calls = 0
        self.first_data = first_data
        self.exception = exception
    def read(self, n):
        self.calls += 1
        if self.calls == 1:
            return self.first_data
        raise self.exception("OOM on second read")

class BadFileno:
    """fileno() raises but object is callable — exposes fallback-path PyErr_Clear."""
    def fileno(self):
        raise MemoryError("OOM in fileno")
    def __call__(self, size):
        return b""
```

**Confirmed on**: awkward-cpp (`fromjsonobj` — MemoryError on second `read()` swallowed by C++ JSON parser, reported as `ValueError: incomplete JSON`), astropy (`IterParser_init` — `BadFileno` MemoryError swallowed by unguarded `PyErr_Clear`)

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

**Triggers**: Any unchecked allocation (`PyType_GenericAlloc`, `PyList_New`, `PyDict_New`, `PyUnicode_FromString`, `PyObject_Malloc`, etc.) that dereferences the result without a NULL check.

**Bug class**: C code calls an allocation function, doesn't check for NULL, and dereferences the result. Under normal conditions these rarely fail, but `_testcapi.set_nomemory(n, 0)` forces all allocations after the `n`-th one to fail, systematically exercising every OOM error path.

### What `set_nomemory` actually hooks

`set_nomemory(start, stop)` installs a counting allocator that replaces **all three** CPython allocator domains: `PYMEM_DOMAIN_RAW` (used by `PyMem_RawMalloc`, interpreter-critical bookkeeping), `PYMEM_DOMAIN_MEM` (used by `PyMem_Malloc`), and `PYMEM_DOMAIN_OBJ` (used by `PyObject_Malloc` / `PyObject_New` / `PyDict_New` / `PyTuple_New` / etc). See `_testcapi/mem.c:120-145` in CPython. This means it reaches every allocation that CPython itself performs — including all pymalloc pool refills, object creation, and temporary string construction.

**What it does NOT hook**: direct calls to `malloc`/`calloc`/`realloc` from C libraries that bypass CPython's allocator (libzstd, HDF5, libxml2, etc.). For those, use Technique 23 (libfiu + `LD_PRELOAD`).

**Count semantics**: `set_nomemory(start, stop)`:
- Resets the internal counter to 0 on each call.
- Increments the counter on every allocation (across all 3 domains).
- Fails the allocation if `count > start AND (stop <= 0 OR count <= stop)`.
- So `set_nomemory(N, 0)` = "fail all allocations from count=N+1 onwards"; `set_nomemory(N-1, N)` = "fail exactly allocation #N".

### Dense sweep vs sparse sweep (critical)

**Always sweep densely.** A sparse sweep (`n ∈ [0, 5, 10, 20, 50, 100, 200]`) can miss narrow crash windows that are a single allocation wide. In a 2026-04-12 wrapt re-audit I hit exactly this trap: a sparse sweep of `import wrapt._wrappers` under OOM showed "no crashes, just MemoryError" because my test points happened to straddle a one-allocation crash window. A subsequent dense sweep of `[0..199]` found a segfault at exactly `start=47` (one iteration out of 200), with all 199 other points producing clean `MemoryError`.

The crash turned out to be in CPython's `_PyFrame_GetLocals` at `Objects/frameobject.c:2290` — a missing NULL check after `_PyFrame_GetFrameObject`, introduced by the PEP 667 implementation and tracked as [gh-146092](https://github.com/python/cpython/issues/146092) (already fixed upstream on 2026-03-18 by commit `e1e4852133e`, backported to 3.13 and 3.14). Not a wrapt bug — just a latent OOM hazard in CPython's `locals()` fast path that any dense OOM sweep of an older 3.13.x or 3.14.x build will hit before reaching the target. The methodology lesson is general: **unchecked allocations can have a window as narrow as one allocation**, and a sparse sweep that skips that value will silently mis-classify the code as safe.

### Subprocess isolation (critical)

**Run each iteration in its own subprocess.** A segfault terminates the Python interpreter, so an in-process loop only reports the FIRST crash and then dies. Worse, pipe-wrapped invocations can silently hide the segfault: `timeout 10 python test.py 2>&1 | head -30; echo $?` reports the exit code of the pipeline (which is `echo`'s 0), not Python's 139. You only discover the crash when running Python directly.

### Correct sweep harness

```python
import subprocess
import sys

TEMPLATE = r"""
import _testcapi, faulthandler
faulthandler.enable()  # prints C stack on SIGSEGV to stderr
_testcapi.set_nomemory({start}, 0)
try:
    target_function_under_test()
    print("ok")
except MemoryError:
    print("MemoryError")
except BaseException as e:
    print(type(e).__name__)
finally:
    _testcapi.remove_mem_hooks()
"""


def oom_dense_sweep(target_source, max_start=200):
    """Run a dense 0..max_start sweep of set_nomemory over `target_source`.

    `target_source` is a Python source string that defines and invokes
    a function named `target_function_under_test`. Each start value is
    tested in a fresh subprocess so a crash in one does not terminate
    the sweep.

    Returns a list of (start, returncode, last_stdout_line, last_stderr_line).
    """
    results = []
    for start in range(0, max_start):
        script = target_source + TEMPLATE.format(start=start)
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        stdout_tail = (r.stdout.strip().splitlines() or [""])[-1]
        stderr_tail = (r.stderr.strip().splitlines() or [""])[-1]
        results.append((start, r.returncode, stdout_tail, stderr_tail))
        # rc < 0 = killed by signal (most commonly -11 = SIGSEGV)
        # rc == 139 = also SIGSEGV (128 + 11) depending on shell convention
        # rc == 1 = clean Python exception exit
        if r.returncode < 0 or r.returncode == 139:
            print(f"CRASH at start={start}: rc={r.returncode}")
            print(f"  stderr tail: {stderr_tail[:120]}")
    return results


TARGET = r"""
import wrapt._wrappers
def target_function_under_test():
    fw = wrapt._wrappers._FunctionWrapperBase.__new__(
        wrapt._wrappers._FunctionWrapperBase)
    fw(1, 2, 3)  # crash site: lazy-init guard missing
"""
results = oom_dense_sweep(TARGET, max_start=200)

crashes = [r for r in results if r[1] < 0 or r[1] == 139]
print(f"\n{len(crashes)} crash(es) out of {len(results)}")
for start, rc, out, err in crashes:
    print(f"  start={start} rc={rc}")
```

### Module-init OOM testing

If the target is a **module's init code** (e.g., `wrapt_exec`, `PyInit_*`), you cannot use `del sys.modules[...]; import ...` in the same process to re-trigger init — CPython caches the `dlopen`'d `.so` at the dynamic linker level, and the subsequent `import` returns the existing module reference without re-running init. Use subprocess isolation instead: arm `set_nomemory` BEFORE the `import` statement in a fresh child, and each subprocess gets a fresh `dlopen` + fresh module init.

### Common pitfalls

1. **Sparse sweeps miss narrow windows.** Always sweep densely (every integer from 0 upward), not samples. One-allocation-wide crash windows exist and are invisible to `[0, 5, 10, 20, 50, ...]` sampling.
2. **Piped shell invocations hide segfaults.** `python test.py 2>&1 | head` reports the pipeline's exit status, not Python's. Run Python directly or capture `subprocess.run(...).returncode` in a parent process.
3. **In-process loops only catch the first crash.** A segfault terminates the interpreter. Use subprocess-per-iteration.
4. **`faulthandler.enable()` must be called BEFORE arming the hook.** Otherwise the signal handler installation itself may allocate and fail.
5. **`_testcapi.remove_mem_hooks()` in `finally`.** Without it, subsequent Python cleanup (exception formatting, frame destruction) runs under the hook and can cascade-fail.
6. **Exception formatting allocates.** Under aggressive OOM, `except MemoryError as e: print(f"{type(e).__name__}: {e}")` can itself raise `MemoryError` inside the f-string construction. Keep error logging allocation-free: `print("MemoryError")` instead of `print(f"MemoryError: {e}")`.
7. **Not all crashes are the target's fault.** The wrapt re-audit crash at `start=47` was initially mis-attributed to setuptools's `_distutils_hack.DistutilsMetaFinder.find_spec` (because that's what `faulthandler` named in the Python traceback), but narrowing showed the real root cause was CPython itself — a missing NULL check in `_PyFrame_GetLocals` at `Objects/frameobject.c:2290` under OOM via the PEP 667 frame-locals-proxy path. Tracked as [gh-146092](https://github.com/python/cpython/issues/146092) and already fixed upstream. When you find a crash, always (a) confirm it reproduces with a DIFFERENT target module under the same OOM budget, (b) narrow the reproducer until it has zero third-party dependencies, and (c) check that your Python interpreter is past any known OOM-hardening fixes.

8. **Your Python interpreter may itself have OOM bugs.** Any dense `set_nomemory` sweep is effectively a fuzz test against CPython's own error-path coverage. Running on a slightly-old CPython build can surface crashes that have nothing to do with your target. Always update to the latest patch release of your Python version before concluding that a crash is in the extension under test.

### Exit code meanings

- `rc == 0`: target succeeded — the OOM budget was larger than the total allocations the target makes. Either a very short path or a start value past the last allocation.
- `rc == 1`: target raised an exception (usually `MemoryError`) and Python exited cleanly. **This is the safe path.**
- `rc == 139`: SIGSEGV exit code via shell convention (128 + 11). Unchecked allocation, NULL deref.
- `rc == 134`: SIGABRT (128 + 6). Often `assert()` failure or CPython's debug-build assertions (`Py_FatalError`, `_PyObject_AssertFailed`).
- `rc < 0`: Python `subprocess` returns negative exit codes for signal-killed children (e.g. `-11` for SIGSEGV, `-6` for SIGABRT). Depending on platform and shell, the same crash shows as `139` or `-11`.

**How it works**: `_testcapi.set_nomemory(n, 0)` installs a custom memory allocator that lets the first `n` allocations succeed, then returns NULL for all subsequent ones. It systematically tests every allocation point in the code path. When an unchecked allocation fails, the NULL return is dereferenced, causing a segfault.

**Requirements**: CPython with `_testcapi` module (standard in CPython builds, not available in all distributions — check with `import _testcapi; _testcapi.set_nomemory`). `_testcapi` is disabled in stripped production builds and in some distro packages.

**Confirmed on**:
- traits (`get_trait` unchecked `PyType_GenericAlloc` — segfault, dense sweep)
- wrapt v1 (Finding 7: unchecked `PyDict_New` in `WraptObjectProxy_new` — dense sweep)
- wrapt v2 (Findings #29, #41 reproduced via dense sweeps under set_nomemory in re-audit 2026-04-12)

**Collateral finding surfaced by this technique**: dense sweep of `import wrapt._wrappers` discovered a segfault at `start=47`, initially thought to be in `_distutils_hack.DistutilsMetaFinder.find_spec` (setuptools) based on the faulthandler traceback, with C stack in `PyTuple_Pack+0x115`. Narrowing the reproducer to 11 lines with zero third-party dependencies pinned the real root cause to a missing NULL check in CPython's `_PyFrame_GetLocals` at `Objects/frameobject.c:2290` — a PEP 667 `locals()` fast path that fails to handle `_PyFrame_GetFrameObject` returning NULL under OOM. Tracked as [gh-146092](https://github.com/python/cpython/issues/146092), reported via `cpython-review-toolkit`, fixed upstream in main/3.14/3.13 on 2026-03-18 by commit `e1e4852133e` (PR #146124). The setuptools attribution was a red herring: the `.format(**locals())` line of `_distutils_hack` happened to be the first `locals()` call in the import dispatch path, so that's where `faulthandler` reported it — but any `locals()` call under OOM in an affected CPython build crashes identically.

---

## Technique 19: Stateful metaclass hash for type-keyed dict lookups

**Triggers**: `PyDict_GetItem` (or similar) that uses a Python type as a dict key. The type's `__hash__` is controlled by its metaclass. A stateful metaclass can make `__hash__` succeed during dict insertion but fail during lookup.

**Bug class**: C code uses `PyDict_GetItem(dict, type_obj)` to look up a type in a registry. `PyDict_GetItem` silently swallows exceptions from `__hash__`. If `__hash__` raises `MemoryError`, the lookup silently fails and the registered handler is skipped.

```python
class StatefulHashMeta(type):
    _fail = False
    def __hash__(cls):
        if StatefulHashMeta._fail:
            raise MemoryError("hash bomb — delayed")
        return type.__hash__(cls)

class DelayedBrokenType(metaclass=StatefulHashMeta):
    pass

# Register DelayedBrokenType in a type registry (hash succeeds here)
registry = {DelayedBrokenType: "handler"}

# Arm the bomb — hash now fails during LOOKUP
StatefulHashMeta._fail = True

# PyDict_GetItem(registry, DelayedBrokenType) silently swallows MemoryError
# and returns NULL as if the key were not found
```

**How it works**: Python types get their `__hash__` from their metaclass. A stateful metaclass can control when hashing fails. This is the key insight: the type is used as a dict key in C extensions' type registries (e.g., bson's encoder/decoder maps, custom serializers). The hash works during registration but fails during lookup, making `PyDict_GetItem` silently drop the registered handler.

**Confirmed on**: pymongo/bson `_write_element_to_buffer` type registry lookup — `MemoryError` silently swallowed, `InvalidDocument` raised instead of `MemoryError`.

---

## Technique 20: str subclass in `sys.modules` for `PyDict_GetItem` error injection

**Triggers**: `PyDict_GetItem(sys.modules, module_name)` where the C code uses `PyDict_GetItem` (which silently swallows exceptions) instead of `PyDict_GetItemWithError`.

**Bug class**: C code looks up a module in `sys.modules` via `PyDict_GetItem`. If a str subclass with a raising `__eq__` is inserted as a key, the lookup triggers `__eq__` during hash collision resolution. `PyDict_GetItem` silently clears the exception and returns NULL ("not found"). If the C code then dereferences the NULL without proper checking, it crashes. This turns a "safe" `PyDict_GetItem` call into a segfault when combined with a downstream NULL-check bug.

```python
import sys

class PoisonStr(str):
    """str subclass whose __eq__ raises during dict lookup."""
    def __eq__(self, other):
        raise MemoryError("injected OOM during dict key comparison")
    def __hash__(self):
        # Must match the hash of the target module name to trigger __eq__
        return hash("target_module_name")

# Insert the poison key into sys.modules
# The hash matches "target_module_name", so any lookup for that module
# will compare against our PoisonStr, triggering __eq__
sys.modules[PoisonStr("target_module_name")] = None

# Now any C code that does PyDict_GetItem(sys.modules, "target_module_name")
# will hit the PoisonStr's __eq__, which raises MemoryError.
# PyDict_GetItem silently swallows it and returns NULL.
# If the C code has a bug in its NULL check, it crashes.
```

**How it works**: `sys.modules` is a regular Python dict. Dict lookups compare keys using `__eq__` when hash values collide. By inserting a str subclass whose `__eq__` raises, we make the lookup fail with an exception that `PyDict_GetItem` silently swallows. The C code receives NULL and — if it has a downstream bug like a typo in the NULL check variable name — dereferences it.

This technique is especially powerful for exposing **compound bugs**: the `PyDict_GetItem` error swallowing alone is not exploitable, but combined with a separate NULL-check bug (wrong variable, missing check, etc.), it becomes a segfault.

**Confirmed on**: msgspec (`structmeta_get_module_ns` — `PyDict_GetItem` error swallowing + typo in NULL check variable name → segfault)

---

## Technique 21: Mischievous file-like objects for I/O code

**Triggers**: C/C++ code that reads from Python file-like objects via `obj.read()`, `obj.write()`, `obj.seek()`, `obj.fileno()`, or `os.fspath(obj)`.

**Bug class**: Three distinct patterns:
1. **Wrong return type**: `read()` returns `int` instead of `bytes` — exposes missing type checks
2. **Delayed failure**: First `read()` succeeds (returning partial data), second `read()` raises — exposes mid-operation error handling where the exception propagates through C code that doesn't expect Python exceptions
3. **Method raises but object has fallback path**: `fileno()` raises but object is callable — exposes unguarded `PyErr_Clear` in fallback chains

```python
# Pattern 1: Wrong return type
class WrongTypeFile:
    def read(self, n):
        return 42  # Not bytes — triggers type check

# Pattern 2: Delayed failure (most powerful)
class DelayedOOMFile:
    def __init__(self):
        self.calls = 0
    def read(self, n):
        self.calls += 1
        if self.calls == 1:
            return b'partial data here'
        raise MemoryError("OOM on second read")

# Pattern 3: Fallback path exploitation
class FallbackFile:
    def fileno(self):
        raise MemoryError("OOM in fileno")
    def __call__(self, size):
        return b""  # Callable fallback path
```

**How it works**: I/O code typically has a "try C file descriptor, fall back to Python read()" pattern. Pattern 3 exploits this: `fileno()` fails with MemoryError, the code falls through to the callable check (which succeeds), and an unguarded `PyErr_Clear` swallows the MemoryError.

Pattern 2 is the most powerful for C++ extensions: the first successful read starts a parsing operation (JSON, XML, etc.), then the second read raises inside the C++ parser's callback. The C++ code typically doesn't distinguish "end of file" from "Python exception during read", so the MemoryError gets converted to a parse error.

**Confirmed on**:
- awkward-cpp (`fromjsonobj` — DelayedOOMFile MemoryError converted to `ValueError: incomplete JSON object`)
- astropy (`IterParser_init` — FallbackFile MemoryError swallowed by unguarded `PyErr_Clear` in fileno→callable fallback)

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
| 18. OOM injection (pymalloc) | Unchecked allocations in Python allocator hierarchy | `_testcapi.set_nomemory` | Easy |
| 19. Stateful metaclass hash | PyDict_GetItem swallows errors on type-keyed lookups | Metaclass | Medium |
| 20. str subclass in sys.modules | PyDict_GetItem error injection + compound NULL bugs | str subclass | Hard |
| 21. Mischievous file-like objects | I/O error handling, mid-parse exception swallowing | Custom class | Easy |
| 22. Callback modifying caller state | Double-free / UAF via user-callback state mutation | Custom callback | Hard |
| 23. System-malloc failure injection | Unchecked `malloc`/`calloc`/`realloc` in extensions or foreign C libs | libfiu + LD_PRELOAD | Medium |

---

## Technique 22: Callback that modifies caller's internal state

**Triggers**: Double-free, use-after-free, refcount underflow, or data corruption when a C extension calls a user-provided Python callback that modifies the extension's own data structures (markers dicts, caches, internal containers) during the call.

**Bug class**: C code assumes its internal state is unchanged across a callback invocation. When the callback modifies that state (clears a dict, removes an item, replaces a reference), the C code's post-callback cleanup operates on stale or missing data.

**How it works**: Many C extensions pass user-provided callbacks (default handlers, key functions, comparison functions, visitor functions) while holding references to internal data structures. If the callback has access to those data structures (directly or via the encoder/decoder object), it can mutate them mid-operation.

```python
import simplejson._speedups as sp
import decimal

markers = {}

class Evil:
    pass

call_count = [0]

def bad_default(obj):
    call_count[0] += 1
    if call_count[0] <= 1:
        markers.clear()  # Remove ident from markers mid-encoding!
        return "safe"
    return str(obj)

c_enc = sp.make_encoder(
    markers, bad_default, sp.encode_basestring_ascii, None, ", ", ": ",
    False, False, True, None, False, False, False, None,
    None, "utf-8", False, False, decimal.Decimal, False,
)

c_enc(Evil(), 0)
# Segmentation fault — double Py_XDECREF on ident after
# PyDict_DelItem fails because markers was cleared
```

**Why it works**: The simplejson C encoder stores `ident = PyLong_FromVoidPtr(obj)` in the `markers` dict before calling `default(obj)`. After the callback returns, it tries to remove `ident` from `markers`. If the callback cleared the dict, `PyDict_DelItem` fails, triggering an error path that XDECREF's `ident` — but the unconditional XDECREF after the `if` block decrements it again, causing a use-after-free.

**Variations**:
- Callback that **removes a specific key** from a cache dict (more surgical than `clear()`)
- Callback that **replaces the dict** entirely (if the extension re-reads the reference)
- Callback that **triggers GC** which collects the container (via `gc.collect()` or circular reference creation)
- Callback that **raises an exception** after modifying state (combines state corruption with error-path bugs)
- `__del__` destructor that modifies shared state when an object's refcount hits zero during the callback

**What to look for in code review**: Any pattern where C code:
1. Acquires a reference or inserts into a container
2. Calls a user-provided Python callback
3. Assumes the container/reference is unchanged after the callback returns
4. Performs cleanup (DECREF, dict removal) based on that assumption

First confirmed on: simplejson `encoder_listencode_obj` double-XDECREF on `ident` (Finding 14).

---

## Technique 23: System-malloc failure injection via libfiu

**Triggers**: Unchecked return from `malloc`/`calloc`/`realloc` inside any C code that runs in the Python process — including CPython stdlib extensions, third-party extensions, and any foreign C libraries they link against (libzstd, HDF5, libssl, ICU, libxml2, etc.). Complements Technique 18 which only reaches Python's own allocator hierarchy (`PyMem_Malloc`, `PyObject_Malloc`).

**Bug class**: Extension code (or a foreign C library it calls) allocates via system malloc and either (a) fails to check the return value, or (b) has a partially-implemented error path that crashes on NULL, or (c) treats OOM as a recoverable condition but leaks/corrupts state on the way out. `_testcapi.set_nomemory` cannot reach these sites because it only hooks `PYMEM_DOMAIN_RAW`.

**How it works**: [libfiu](https://blitiri.com.ar/p/libfiu) (public domain) is an LD_PRELOAD-based fault injection library. Its `fiu_posix_preload.so` interposes `malloc`, `calloc`, `realloc`, `mmap`, `open`, `read`, `write`, and ~50 other POSIX functions. At runtime, failure points can be enabled and disabled from Python via the `fiu` bindings. Failure points have names like `libc/mm/malloc`, `posix/mm/mmap`, `posix/io/oc/open`, etc. The control API offers four targeting modes:

- **Unconditional**: `fiu.enable("libc/mm/malloc")` — every call fails until disabled.
- **Probabilistic**: `fiu.enable_random("libc/mm/malloc", probability=0.05)` — 5% chance per call, for chaos testing.
- **External callback**: `fiu.enable_external("libc/mm/malloc", cb)` — arbitrary Python predicate per call (fail the Nth, fail after a flag, count-and-decide, etc.).
- **Call-site-specific**: `fiu.enable_stack_by_name("libc/mm/malloc", func_name="ZSTD_createCCtx")` — only fail when the named C function is on the call stack. Uses `backtrace()` + `dlsym()`, so the target function must be in the dynamic symbol table.

### Setup

1. Clone and build libfiu with a local prefix (no sudo needed):
    ```
    cd ~/projects && git clone https://blitiri.com.ar/repos/libfiu
    cd libfiu && make PREFIX=$HOME/projects/libfiu/install
    make install PREFIX=$HOME/projects/libfiu/install
    ```
2. Build the Python bindings inside the venv:
    ```
    source ~/venvs/your-venv/bin/activate
    cd bindings/python
    LDFLAGS="-L$HOME/projects/libfiu/install/lib -Wl,-rpath,$HOME/projects/libfiu/install/lib" \
    CPPFLAGS="-I$HOME/projects/libfiu/install/include" \
    python setup.py install
    ```
3. Set `LD_LIBRARY_PATH` + `LD_PRELOAD` before running Python:
    ```
    export LD_LIBRARY_PATH=$HOME/projects/libfiu/install/lib
    export LD_PRELOAD=$HOME/projects/libfiu/install/lib/fiu_run_preload.so:$HOME/projects/libfiu/install/lib/fiu_posix_preload.so
    ```
   Or wrap the invocation with `fiu-run -x` which sets these for you.

### Usage

Use the scoped helpers in `docs/libfiu_helpers.py` (the catalog's companion module) rather than raw `fiu.enable()` — the bare API is a footgun because an unconditional enable under `PYTHONMALLOC=malloc` can brick the interpreter (even `fiu.disable()` needs to allocate).

```python
import libfiu_helpers as fh
import compression.zstd as czstd

fh.require_preloaded()
fh.promote_to_global("libzstd.so.1")  # needed for call-site targeting

# Fail ONLY the malloc inside ZSTD_createCCtx, nothing else.
with fh.from_stack_of("libc/mm/malloc", func_name="ZSTD_createCCtx"):
    try:
        czstd.ZstdCompressor()
    except czstd.ZstdError as e:
        print(f"Error path fired cleanly: {e}")
        # -> "Unable to create ZSTD_CCtx instance."
```

Or for coarser targeting via a counting predicate:

```python
# Fail the 3rd malloc inside the protected region.
with fh.nth_allocation("libc/mm/malloc", n=3) as state:
    some_function_that_allocates_multiple_times()
assert state["failed_at"] == [3]
```

### Gotchas

1. **`ctypes.CDLL("libc.so.6")` bypasses LD_PRELOAD.** Explicit `dlopen` of a named library gets a direct handle to that library; `dlsym()` on that handle returns the real symbol, not the preloaded interposition. Use `ctypes.CDLL(None)` (RTLD_DEFAULT) when you want the preload chain to win.

2. **Pymalloc pool allocations bypass `libc/mm/malloc`.** For Python objects ≤ 512 bytes, pymalloc serves them from arena pools without calling `malloc`. Larger objects fall through to the raw allocator and ARE intercepted. If you need to reach all Python allocations (including small ones), set `PYTHONMALLOC=malloc` — but see gotcha 3.

3. **Unconditional `enable` under `PYTHONMALLOC=malloc` bricks the interpreter.** Every subsequent malloc fails, including the allocation that `fiu.disable()` itself needs. Symptom: "MemoryError (no message)" followed by "lost sys.stderr". **Always** use one of the scoped helpers (`nth_allocation`, `enable_if`, `from_stack_of`) or `FIU_ONETIME`, never bare `fiu.enable()`.

4. **Extension modules are loaded with `RTLD_LOCAL` by default.** That means symbols from shared libraries they link against (like libzstd) are NOT visible to `dlsym(RTLD_DEFAULT, ...)`. Before using `from_stack_of` against a symbol in one of those libraries, call `fh.promote_to_global("libzstd.so.1")` to explicitly dlopen the library with `RTLD_GLOBAL`. Do this BEFORE importing the extension module.

5. **Inlined / hidden-visibility functions don't show up on the backtrace.** `from_stack_of` walks glibc's `backtrace()`, which skips `static inline` functions and functions built with `-fvisibility=hidden`. Most CPython-internal functions (`PyEval_*`, `_PyObject_*`) are exported, so targeting works. For third-party extensions, check with `nm` or `objdump -T` that the symbol is present in `.dynsym`.

6. **`ctypes.CDLL("libzstd.so.1", mode=ctypes.RTLD_GLOBAL)` must happen before the extension that uses libzstd is imported** — otherwise the first import wins with `RTLD_LOCAL` and the global promotion has no effect on symbol resolution for that library. The `promote_to_global` helper is just a one-liner reminder; the real work is the import-order discipline.

7. **`fiu-run -x -c 'enable name=X'` vs raw LD_PRELOAD**: `fiu-run` is a thin bash wrapper that sets `LD_PRELOAD`, `FIU_ENABLE`, and `FIU_CTRL_FIFO` env vars. It's convenient for one-shots; raw LD_PRELOAD is better for test harnesses where env setup is amortized across many runs.

8. **`fiu-ctrl -c 'enable ...' <pid>` works for remote control** over a named pipe, but the target must either be launched via `fiu-run` (which opens the pipe) or call `fiu.rc_fifo(path_prefix)` from inside the target process. Useful for long-lived servers where you want to toggle failure injection without restarting.

**Confirmed on**:
- CPython 3.14 `_zstd` extension + `libzstd.so.1` — validated that `from_stack_of("libc/mm/malloc", func_name="ZSTD_createCCtx")` surgically fails one malloc inside libzstd and CPython raises `ZstdError: Unable to create ZSTD_CCtx instance.` with no segfault, no leaked state, and no collateral on surrounding allocations. Full reproducer: `t6_zstd_validation.py` (see `docs/libfiu_helpers.py` usage example).

---

## Technique 24: RSS growth monitoring for leaks invisible to tracemalloc

**Triggers**: Per-instance leaks where the leaked memory is allocated by libstdc++'s `malloc`/`new` rather than by CPython's allocator hierarchy. Specifically: `tp_dealloc` that skips destructors of embedded C++ members (`std::shared_ptr` control blocks, `std::unique_ptr` heap buffers, `std::string` SSO overflow, `std::queue`/`std::mutex`/`std::condition_variable` storage); `__init__` that re-allocates a C++ object without freeing the prior one; any code path where `new T(...)` fires without a matching `delete`.

**Bug class**: C++-embedded-member leaks in Python extension types. These are **invisible to Technique 17 (tracemalloc)** because `tracemalloc` only sees allocations that pass through `PyMem_Malloc` / `PyObject_Malloc`. A `std::shared_ptr<T>` constructor calls libstdc++'s `__gnu_cxx::__aligned_membuf` allocator (which eventually calls raw `malloc`), bypassing CPython's allocator entirely. Same for `new T(...)`, `std::make_shared<T>()`, and most STL containers. The leak is real, but tracemalloc reports zero.

**How it works**: `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` returns the process's peak resident-set-size in KiB. Measure it before and after N create-destroy (or re-init) cycles; a monotonic, linear-in-N growth is unambiguous evidence of a leak, and the slope gives the per-operation leak size. The trick is making N large enough that per-iteration leak × N rises above RSS measurement noise (~100-300 KB from Python's own garbage).

### Template

```python
import gc, resource

def rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

# Warmup: let steady-state Python allocations settle.
for _ in range(100):
    obj = TargetType()
    del obj
gc.collect()
rss0 = rss_kb()

N = 10_000
for _ in range(N):
    obj = TargetType()
    del obj
gc.collect()
rss1 = rss_kb()

growth_kb = rss1 - rss0
per_instance_b = (growth_kb * 1024) / N
print(f"{N} cycles: RSS {rss0}→{rss1} KB (+{growth_kb} KB)")
print(f"Per-instance leak: {per_instance_b:.1f} bytes")
```

### Detection threshold

- **Per-cycle leak < 16 B**: below reliable detection. Linux RSS is page-granular (4 KB pages); allocations smaller than ~100 B may never touch a new page in 10k cycles. Use N = 100k or bigger objects.
- **Per-cycle leak 16–100 B**: detectable at N = 10k but signal is noisy. Run 3 times and check consistency.
- **Per-cycle leak > 100 B**: clean signal at N = 10k.
- **Per-cycle leak > 1 KB**: clean signal at N = 1k.

For reference, a default `std::shared_ptr<T>` control block is 16–32 B; a default-constructed `std::mutex` + `std::condition_variable` + `std::queue` combo is ~300–700 B depending on libc/libstdc++ version.

### Gotchas

1. **Run inside a subprocess or fresh interpreter if possible.** Stale cached Python objects (bytecode cache, import state) can inflate RSS0 by MB amounts. Use `subprocess.run([sys.executable, ...])` for a clean baseline.
2. **`ru_maxrss` is high-water-mark, not current.** It never decreases. If the workload does large temporary allocations during warmup, RSS0 will already be high and the slope will be measured on the "additional growth" axis. This is what you want.
3. **Linux reports `ru_maxrss` in KB, macOS in bytes.** Catalog template assumes Linux. On macOS, divide by 1024.
4. **glibc `malloc` has free-list arenas that don't always return memory to the kernel.** Even with 100% correct cleanup, RSS may show a one-time small spike that then stays flat. A leak shows *linear* growth across contiguous N-sized batches. Run multiple batches to distinguish.
5. **Combine with tracemalloc** to confirm the leak is **not** in the CPython allocator. If tracemalloc reports 0 but RSS grows, the leak is in libstdc++/libc (the case this technique targets). If tracemalloc reports growth, use Technique 17's differential-snapshot instead — it gives precise allocation sites.
6. **`gc.collect()` before the measurement closes Python-side cycles** that would otherwise inflate the diff. Always call it. For C++-embedded-member leaks, `gc.collect()` has no effect on the leaked bytes (C++ destructors don't run), which is exactly why the leak shows up.

### When to use tracemalloc (Technique 17) vs this

- **Leak is in a Python-level allocation** (e.g., `PyList_New` result never DECREFed): Technique 17. Gives you the allocation stack.
- **Leak is in a C++ STL member that skips destruction**: Technique 24. Technique 17 reports 0.
- **Unsure**: run both. If Technique 17 shows no growth but Technique 24 does, you know it's a C++-side leak and can stop looking for missing `Py_DECREF`s.

**Confirmed on**:
- couchbase-python-client `pycbc_streamed_result` — 786 B per create-destroy cycle (confirms F12 `shared_ptr<rows_queue>` destructor skip in `tp_dealloc`). Tracemalloc reported 0 bytes growth.
- couchbase-python-client `pycbc_hdr_histogram.__init__` — 4.5 KB per re-init (confirms F18 `hdr_init` second-call leak of prior counts buffer). `hdr_init` uses raw `malloc`, invisible to tracemalloc.

---

## Technique 25: SystemError probe for PyCFunction contract violations

**Triggers**: Any C function exposed via `METH_*` flags that returns a non-NULL `PyObject*` while `PyErr_Occurred()` is true. The two most common patterns:

- `if (!PyArg_ParseTupleAndKeywords(...)) { PyErr_SetString(...); Py_RETURN_NONE; }` — returns `Py_None` instead of `NULL`, with an exception pending.
- `tp_new` that does `PyErr_SetString(...); Py_RETURN_NONE;` on parse failure — returns `Py_None` (which by `tp_new`'s contract should be an instance of the type being created), but with exception set.

**Bug class**: PyCFunction contract violation. CPython's call dispatcher checks, after every C function return, that the `(return_value != NULL) == (PyErr_Occurred() == NULL)` invariant holds. If it doesn't, CPython raises:

```
SystemError: <qualname> returned a result with an exception set
```

This is the **unambiguous signature** of the bug — no other code path in CPython produces this exact message. If you see it, you've caught a PyCFunction-contract violation, every time.

**How it works**: Pass deliberately-malformed argument types to the suspect function. `PyArg_ParseTupleAndKeywords` sets `TypeError` on format-string mismatch; if the function has the buggy `PyErr_SetString + Py_RETURN_NONE` pattern, the return-without-NULL trips CPython's contract check and surfaces as `SystemError`.

### Template

```python
from couchbase.logic.pycbc_core import _core   # or whatever target module

MODULE_FNS = [
    "fn_name_1", "fn_name_2", "fn_name_3",       # fill in the target functions
]

systemerror_count = 0
for fn_name in MODULE_FNS:
    fn = getattr(_core, fn_name)
    # Probe 1: positional garbage
    try:
        fn("garbage")
    except SystemError as e:
        print(f"{fn_name}: SystemError (BUG): {e}")
        systemerror_count += 1
    except TypeError:
        pass   # clean — parse function's TypeError propagated correctly
    except Exception as e:
        print(f"{fn_name}: {type(e).__name__}: {e}")  # unusual
    # Probe 2: bad kwargs (forces a different parse-failure branch)
    try:
        fn(x=1, y=2)
    except SystemError as e:
        print(f"{fn_name}: SystemError (BUG): {e}")
        systemerror_count += 1
    except (TypeError, ValueError):
        pass

print(f"Total SystemError observations: {systemerror_count}")
```

### Why both probes matter

Different parse-failure paths through `PyArg_ParseTupleAndKeywords` can take different error branches inside the C function. A positional call with wrong-type args fails at type-check; a kwargs-only call with unknown keys fails at kwarg-validation; a completely malformed call fails at arity-check. If the function has the bug pattern on *some* paths but not others, one probe will miss while the other catches. Always run at least two.

### What you can't probe this way

- **`tp_init` sites** that return `-1` on error. `tp_init` uses the C-int return convention, not the pointer convention, so it's immune to this specific contract violation. Those bugs produce `ValueError` or whatever exception was set, with no SystemError. They're real bugs (caller's missing error check) but need a different detection technique.
- **Functions whose bug is on the success path** — returning `Py_None` as a valid return value when an error *should* have fired. This technique detects "returned non-NULL with exception set"; it does not detect "returned non-NULL with no exception set when the operation actually failed". For that, you need semantic assertions against the function's docs.

### Gotchas

1. **Python 3.9+ prints a traceback but the SystemError is still catchable.** The `SystemError: ... returned a result with an exception set` message goes to the `except SystemError` clause; the original (intended) exception becomes the `__context__` of the SystemError. Print `e.__context__` if you want to see the masked specific exception.
2. **Some wrappers swallow the SystemError.** If the C function is called via a Cython wrapper or a Python-level `try/except BaseException:`, the SystemError may be converted to something else. Call the C function **directly** from the extension module (e.g., `module._core.transaction_op(...)`) for clean detection, not through its Python facade.
3. **The bug manifests on the very next Python statement after the broken function returns.** Not inside the function call. If you see `SystemError` with a traceback pointing at the `return` or the following line, that's the expected pattern — the bug is in whatever function the `<qualname>` in the error message names.
4. **A variant of this bug returns an *old* return value from a cached state.** If you suspect that rather than `Py_None`, the template still works — the SystemError message changes slightly but still contains "returned a result with an exception set".

### When this technique is the right choice

- Any C extension with many `PyArg_ParseTupleAndKeywords` call sites and a project convention of `PyErr_SetString(...); Py_RETURN_NONE;` in the failure branch. A handful of sites may be correct (using `return NULL`) while neighbors are buggy (using `Py_RETURN_NONE`). This technique catches the buggy ones fast.
- Before shipping a new extension: run a loop that calls every METH_VARARGS|METH_KEYWORDS function with bad args and assert **zero** SystemError observations. Regression-catching CI tool.
- In code-review, as a sanity check: if a reviewer spots one `PyErr_SetString; Py_RETURN_NONE`, grep the file for the pattern and run this technique on every hit.

**Confirmed on**:
- couchbase-python-client `_core` — 8 SystemError observations across `transaction_op`, `transaction_query_op`, `transaction_get_multi_op`, `destroy_transactions` (module-level functions), and 2 of 3 `tp_new` sites (`transaction_config`, `transaction_options`). Matches F2 and F3 findings in the v1 review. The remaining `tp_new` (`transaction_query_options`) has the same bug pattern but requires a different input shape to trip the parse failure; the technique documents how to vary probes to catch this class.

---

## Technique 26: Cyclic-GC threshold coercion

**Triggers**: Timing-dependent races in `tp_traverse` / `tp_clear` paths where an object is GC-tracked but its traversable fields (e.g. `ma_keys`, `ob_item`, or any `PyObject *` member the traverse function walks) haven't been populated yet. Common shapes: subclass-conditional `PyObject_GC_UnTrack` in a constructor, partial `tp_alloc` → init split where `tp_alloc`'s implicit tracking precedes member assignment, destructors that `PyObject_GC_UnTrack` after clearing fields and then call back into Python.

**Bug class**: Race between "GC tracking starts" and "all fields that `tp_traverse` reads are valid". At default thresholds the race wins rarely — a deployed program may never hit it. But the CODE is reachable on every construction; only the specific allocation-count alignment that triggers `collect()` inside the vulnerable window is rare. Coercing the threshold down to `(1, 1, 1)` forces `collect()` on every tracked allocation, turning the rare race into a deterministic crash on iteration 0 or 1.

**How it works**: `gc.set_threshold(1, 1, 1)` means "fire gen-0 collect when gen-0 count > 1". Every call to `PyObject_GC_New` / `_PyObject_GC_Alloc` increments the gen-0 count and may fire `collect()`. During `collect()`, every currently-tracked object's `tp_traverse` is invoked. If the target object is tracked but its fields aren't populated, traverse derefs a NULL pointer and SIGSEGVs.

### Template

```python
import gc, my_extension

class MySubclass(my_extension.BaseType):
    """Subclass — some extensions skip GC UnTrack on the subclass path."""

# Coerce: gen-0 collect fires on every tracked allocation.
gc.set_threshold(1, 1, 1)

# Construct via a path that allocates GC-tracked objects (iter, tuple,
# list, dict, etc.) during the partial-init window.
for i in range(100):
    MySubclass([("k", i), ("v", i + 1)])  # should SEGV within 1-2 iterations
```

### Detection

- **SIGSEGV on iteration 0 or 1** → bug confirmed, race window is open during construction.
- **Completes 1000+ iterations** → either no race, or the path doesn't allocate tracked objects during the vulnerable window (try a different construction path — e.g. sequence-of-pairs vs dict-fast-path, non-dict-mapping vs dict).
- **RecursionError / MemoryError** → the threshold is too aggressive and cascading; back off to `(10, 10, 10)` or `(100, 10, 10)`.

### Gotchas

1. **Run with `PYTHONUNBUFFERED=1`.** The SIGSEGV happens mid-construction; any buffered stdout / stderr is lost. Use unbuffered so you see how many iterations survived before the crash.
2. **Restore the threshold in a `finally` block** if the reproducer continues after a handled exception — the aggressive setting also slows unrelated tests.
3. **Newly-allocated objects aren't walked during the collect that was triggered by their own allocation.** The tracking step happens AFTER `_PyObject_GC_Alloc` returns. So the crash is triggered not by the construction's OWN `tp_alloc`, but by the NEXT tracked allocation inside the construction (a tuple, an iterator, a list). The victim is the previous iteration's partially-init object still alive on the stack.
4. **Happens only with `Py_TPFLAGS_HAVE_GC`.** Non-GC types are never walked; this technique doesn't apply.
5. **Default thresholds `(700, 10, 10)` are too loose** to reliably trigger in reasonable wall time even with the bug present; `(1, 1, 1)` is the sweet spot for deterministic detection without breaking the interpreter.

### When to use this vs running the test suite

- The test suite is unlikely to construct an extension subclass with enough GC pressure to trigger the race. This technique is specifically for catching the class of bug where "tests pass, production crashes under unusual GC alignment".
- If a scanner flagged `PyObject_GC_UnTrack(obj)` inside a conditional or inside a constructor's success path, run this technique on a subclass of the target type. It'll confirm or dismiss in 30 seconds.

**Confirmed on**:
- frozendict 2.4.7 `frozendict_new_barebone` subclass UnTrack gap (`c_src/3_10/frozendictobject.c:1422`). `MyFD(frozendict.frozendict)([("k", 0), ("v", 1)])` SIGSEGVs deterministically within iteration 0 or 1 at threshold `(1, 1, 1)`. Reproducer: `reports/frozendict_v1/reproducers/repro_f12_subclass_gc_crash.py`. 5/5 deterministic across runs.

---

## Technique 27: Subprocess-isolated dense OOM sweep

**Triggers**: Unchecked allocation returns in extension code. A function does `ptr = some_alloc(...); use(ptr->field);` without `if (!ptr) return NULL;`. When the allocation fails (returns NULL), the next dereference segfaults. Multiple sites of the same pattern across different code paths. The exact malloc offset that triggers each site varies per path, and we don't know a priori which offset targets which site.

**Bug class**: Missing NULL checks on `PyObject_New` / `PyObject_GC_New` / `PyTuple_New` / `PyDict_New` / internal allocators like `new_keys_object`. Technique 18 (`_testcapi.set_nomemory`) and Technique 23 (libfiu surgical injection) target single sites. This technique is for when you suspect multiple unchecked sites across a family of code paths and want to cast a wide net.

**How it works**: Launch each target code path in its OWN subprocess with libfiu preloaded and `fiu.enable("libc/mm/malloc")` set to fail every subsequent malloc. Classify the subprocess exit code:
- 139 = SIGSEGV (crash — bug)
- 134 = SIGABRT (abort — bug)
- 10 = clean `MemoryError` raised (good — bug absent on this path)
- 0 = completed (either no bug, or the tuple/free-list short-circuited the allocation)

Because each run is isolated, a crash in path A doesn't contaminate path B. Run many offsets in parallel and aggregate.

### Template

```python
import os, subprocess
from pathlib import Path

LIBFIU = Path.home() / "projects/libfiu/install/lib"
TARGET_PY = "/path/to/python"

ENV = {
    **os.environ,
    "PYTHONMALLOC": "malloc",       # disable pymalloc so libc mallocs are hit
    "LD_LIBRARY_PATH": str(LIBFIU),
    "LD_PRELOAD": f"{LIBFIU}/fiu_run_preload.so:{LIBFIU}/fiu_posix_preload.so",
}

def run(label: str, code: str) -> None:
    proc = subprocess.run([TARGET_PY, "-c", code], env=ENV,
                          capture_output=True, text=True, timeout=15)
    verdict = (
        "SIGSEGV (bug)"          if proc.returncode == 139
        else "SIGABRT (bug)"     if proc.returncode == 134
        else "clean MemoryError" if "MemoryError" in proc.stderr
        else f"exit={proc.returncode}"
    )
    print(f"{label}: {verdict}")

# Each target path in its own subprocess
PATHS = [
    ("A: ext.construct({'a':1})",
     "import fiu, ext; fiu.enable('libc/mm/malloc'); ext.construct({'a':1})"),
    ("B: ext.construct(a=1, b=2)",
     "import fiu, ext; fiu.enable('libc/mm/malloc'); ext.construct(a=1, b=2)"),
    ("C: ext.construct([('a',1),('b',2)])",
     "import fiu, ext; fiu.enable('libc/mm/malloc'); ext.construct([('a',1),('b',2)])"),
    # ... more target paths ...
]

for label, code in PATHS:
    run(label, code)
```

For a dense *offset* sweep (not just "fail everything from point X"), use the `nth_allocation` helper from `docs/libfiu_helpers.py` — run the same target code 30 times with N = 1, 2, 3, …, and record which offsets produce which verdicts. That tells you which specific allocations in the target path are unchecked.

### Gotchas

1. **`subprocess.CompletedProcess.returncode` on Linux returns `-N` for signal N**, not `128+N` / `139`. Shell gives 139; Python's subprocess gives `-11`. Test for both `== 139` and `== -11` if you're capturing via subprocess.
2. **`PYTHONMALLOC=malloc` is load-bearing.** Without it, pymalloc intercepts small allocations (< 512 B) before libc; libfiu never sees them. This masks most extension-code allocations, which are small.
3. **Tuple/list/dict free-lists further mask allocations.** CPython caches small tuples (size ≤ 19), lists, and dicts. Even with `PYTHONMALLOC=malloc`, a `PyTuple_New(2)` in hot code may pull from the free-list without calling malloc. If your target allocates a small tuple and the sweep consistently completes without crashing, suspect the free-list is hiding the bug. Use a debug build (no free-lists) or prime a clean interpreter state.
4. **Subprocess startup is ~100-200 ms.** A sweep of 30 offsets × 6 paths is ~30 s wall time. Worth the isolation cost.
5. **Output gets lost on SIGSEGV.** The crashed subprocess's stdout may or may not be flushed before the segfault. Rely on exit code, not stdout, for the verdict. Use `capture_output=True` to at least capture what made it out.
6. **Fresh `fiu.enable("libc/mm/malloc")` after every failed path** — libfiu state persists within a single process. That's why each path lives in its own subprocess: no need to reset state, and crashes don't leak into the next target.

### When to use this vs Technique 18 (set_nomemory) or Technique 23 (libfiu surgical)

- **Technique 18**: for CPython-allocator bugs (pymalloc-visible). Doesn't reach foreign allocators.
- **Technique 23**: for surgical injection — "fail ONLY the malloc inside function X". Best when you have one specific site.
- **Technique 27** (this one): for scanning many sites / many paths at once. Best when you have multiple suspects and want a parallel sweep. Complements T23 rather than replacing it.

**Confirmed on**:
- frozendict 2.4.7 F7 + F8 (`frozendictobject.c:1514, 1533, 1596`; `c_src/3_10/frozendictobject.c:197, 431, 483, 544`) — 4 construction paths × 30 sweep offsets per path, all 120 runs SIGSEGV. Paths: `frozendict({'a':1})`, `frozendict(a=1, b=2)`, `frozendict([('a',1),('b',2)])`, `frozendict(UserDict({'a':1}))`. Reproducer: `reports/frozendict_v1/reproducers/repro_f7_f8_oom_crash.py`.

---

## Technique 28: ctypes struct-field probe via id + offset

**Triggers**: Refcount leaks on internal CPython structs not exposed via Python API — specifically `Py_EMPTY_KEYS.dk_refcnt`, interned-string counts, `PyDictObject.ma_version_tag`, any `Py_ssize_t` or pointer field in a process-wide singleton's internal layout.

**Bug class**: A bug increments (or fails to decrement) a refcount on a singleton that users can't see directly from Python. `sys.getrefcount` only works on Python-visible objects. For `Py_EMPTY_KEYS` (the singleton shared by all empty dicts) or internal dict-keys-table counters, you need to read the raw memory.

**How it works**: Compute `id(python_obj) + field_offset`, cast to `ctypes.POINTER(<field_type>)`, dereference to read. Works because `id(obj)` returns the object's memory address in CPython; the struct layout of `PyDictObject`, `PyDictKeysObject`, `PyTupleObject`, etc. is stable within a CPython minor version. Read before N operations, read after, compute the delta.

### Template

```python
import ctypes

def dk_refcnt_of(d: dict) -> tuple[int, int]:
    """Read dk_refcnt from the (frozen)dict's ma_keys PyDictKeysObject.

    PyDictObject layout (CPython 3.10, 64-bit):
      PyObject_HEAD               16 bytes
      Py_ssize_t ma_used           8 bytes
      uint64_t ma_version_tag      8 bytes  -> ma_keys at offset 32
      PyDictKeysObject *ma_keys
      PyObject **ma_values
    PyDictKeysObject layout:
      Py_ssize_t dk_refcnt         offset 0
    """
    # Pointer to the ma_keys pointer
    ma_keys_ptr = ctypes.cast(id(d) + 32, ctypes.POINTER(ctypes.c_ssize_t))
    keys_addr = ma_keys_ptr[0]
    dk_refcnt = ctypes.cast(keys_addr, ctypes.POINTER(ctypes.c_ssize_t))[0]
    return keys_addr, dk_refcnt

# Anchor: grab the singleton-holding object
empty_fd = ext.frozendict()
addr, baseline = dk_refcnt_of(empty_fd)
print(f"baseline dk_refcnt: {baseline}")

N = 10_000
for _ in range(N):
    ext.frozendict.fromkeys(range(3))    # operation that may leak

_, after = dk_refcnt_of(empty_fd)
per_call = (after - baseline) / N
print(f"delta: {after - baseline}  per-call: {per_call:.3f}")
```

### Detection

- **Per-call delta == 1.000** (or any integer) → bug confirmed; the field is being incremented without matching decrement (or vice versa).
- **Per-call delta ≈ 0.000** → either no leak, or the operation doesn't touch this particular singleton. Check the code to verify you picked the right struct/field.
- **Per-call delta noisy (e.g. 0.5 ± 0.2)** → the field is touched by many code paths, not just your N operations; use a more isolated test.

### Gotchas

1. **Struct layouts change across CPython versions.** Offset 32 for `ma_keys` works on 3.10 but may shift on 3.12 (PEP 699 removed `ma_version_tag`, shrinking the struct by 8 bytes → `ma_keys` at offset 24 on 3.12+). Always verify against `Include/cpython/dictobject.h` for your target version.
2. **Architecture-dependent.** 32-bit systems have 4-byte pointers and Py_ssize_t; offsets halve. Template assumes 64-bit.
3. **Some extensions fork CPython's internal structs.** e.g. frozendict `#include`s its own modified `dictobject.c` with its OWN `Py_EMPTY_KEYS` sentinel (different address from the regular dict's). Verify by reading `id(empty_dict)` vs `id(empty_fd)` — if ma_keys addresses differ, you're looking at fork-specific state.
4. **No bounds checking.** If the offset is wrong, you read garbage or segfault the reader process. Use in a subprocess for safety when testing against an unfamiliar struct.
5. **Works best on singleton / process-wide state.** For per-instance refcounts, `sys.getrefcount(obj)` is simpler and doesn't need offset arithmetic.
6. **The technique is allergic to stripped binaries and to `-fvisibility=hidden`.** The struct layout is defined by the header file, not exported as a symbol, so visibility doesn't matter — but if CPython is built with a non-standard layout (e.g. extra padding for a debug build), offsets may differ. Test against a known-good baseline first.

### When to use vs sys.getrefcount / tracemalloc

- **`sys.getrefcount(obj)`**: for ordinary Python-visible objects. Always prefer this when the leaked object has a Python reference you can hold.
- **tracemalloc**: for total-memory tracking. Doesn't expose individual refcounts.
- **Technique 28** (this one): for singletons / internal structs that users can't directly reach. `Py_EMPTY_KEYS.dk_refcnt`, keys-table counters, interned-string slots.

**Confirmed on**:
- frozendict 2.4.7 F13 `fromkeys` Py_EMPTY_KEYS leak (`c_src/3_10/frozendictobject.c:197`). `frozendict.fromkeys(range(3))` increments frozendict's own (NOT regular dict's) `Py_EMPTY_KEYS.dk_refcnt` by exactly 1 per call — 10,000 calls produce `delta = 10000` exactly. Reproducer: `reports/frozendict_v1/reproducers/repro_f13_fromkeys_empty_keys_leak.py`.

---

## Technique 29: MRO unbound-method bypass enumeration

**Triggers**: Incomplete immutability (or validation) overrides in Python classes that inherit from a mutable base type. Common pattern: `class Frozen(SomeMutableBase): def __setitem__(self, k, v): raise TypeError(...)` — the Python-level override blocks `fd[k] = v` (bound-method call), but `SomeMutableBase.__setitem__(fd, k, v)` (unbound-method call) bypasses the override entirely and mutates the instance.

**Bug class**: Trust-boundary failure in "immutable" Python wrappers. If the class is a *subclass* of a mutable type (inheritance), any caller with a reference can invoke the base class's unbound methods and mutate the "frozen" instance. Only composition (wrapping a mutable instance as an attribute) closes this gap. Affects frozendict, SortedDict-like libraries, any "readonly view" class that uses inheritance for isinstance compatibility.

**How it works**: Walk `type(instance).__mro__`, enumerate every method on each base class that mutates the instance (for dict: `__setitem__`, `__delitem__`, `update`, `clear`, `pop`, `popitem`, `setdefault`, `__ior__`, `__init__`, etc.). For each, probe with `Base.method(instance, *args)` and compare the instance state before vs after. Any observable mutation is a bypass route.

### Template

```python
def find_bypasses(cls, probe_factory, routes):
    """
    cls: the "frozen" class under test.
    probe_factory: callable that returns a fresh instance in a known state.
    routes: list of (label, callable_taking_probe_instance).
    Returns: list of routes that observably mutated the probe.
    """
    bypassed = []
    for label, op in routes:
        probe = probe_factory()
        before = dict(probe)        # or list(probe), or probe.copy(), etc.
        try:
            op(probe)
        except Exception:
            continue                # override caught it — good
        after = dict(probe)
        if before != after:
            bypassed.append(label)
            print(f"  {label!r}: MUTATED  {before} -> {after}")
    return bypassed

# Example: all 14 dict-descriptor routes against frozendict
ROUTES = [
    ("dict.__setitem__",        lambda fd: dict.__setitem__(fd, "k", "v")),
    ("dict.__delitem__",        lambda fd: dict.__delitem__(fd, "a")),
    ("dict.update (mapping)",   lambda fd: dict.update(fd, {"b": "updated"})),
    ("dict.update (pairs)",     lambda fd: dict.update(fd, [("p", "q")])),
    ("dict.update (kwargs)",    lambda fd: dict.update(fd, kw="v")),
    ("dict.clear",              lambda fd: dict.clear(fd)),
    ("dict.pop",                lambda fd: dict.pop(fd, "a", None)),
    ("dict.popitem",            lambda fd: dict.popitem(fd)),
    ("dict.setdefault",         lambda fd: dict.setdefault(fd, "nk", "d")),
    ("dict.__ior__",            lambda fd: dict.__ior__(fd, {"i": "v"})),
    ("dict.__init__ (mapping)", lambda fd: dict.__init__(fd, {"init": 1})),
    ("dict.__init__ (kwargs)",  lambda fd: dict.__init__(fd, kw=1)),
    ("super().__init__",        lambda fd: super(frozendict, fd).__init__({"s": 1})),
]

bypassed = find_bypasses(
    frozendict,
    probe_factory=lambda: frozendict({"a": 1, "b": 2}),
    routes=ROUTES,
)
print(f"\nTotal bypass routes found: {len(bypassed)}")
```

### Where to look for routes

Enumerate each class in `type(instance).__mro__` and list its dunder / mutation methods. Useful starter per base type:

- **dict**: `__setitem__`, `__delitem__`, `update`, `clear`, `pop`, `popitem`, `setdefault`, `__ior__`, `__init__`.
- **list**: `__setitem__`, `__delitem__`, `append`, `extend`, `insert`, `remove`, `pop`, `clear`, `sort`, `reverse`, `__iadd__`, `__imul__`, `__init__`.
- **set**: `add`, `update`, `discard`, `remove`, `pop`, `clear`, `intersection_update`, `difference_update`, `symmetric_difference_update`, `__ior__`, `__iand__`, `__isub__`, `__ixor__`, `__init__`.

Also probe `super(Frozen, instance).__init__(...)` — the descriptor protocol's own route can bypass overrides even when the direct unbound call is blocked.

### Gotchas

1. **Hash caches may go stale but not invalidate.** If the frozen class caches `hash(self)` on first call, mutations via bypass routes leave the cache stale. Detect by computing `hash(fd)`, mutating, re-computing. Reproducing this corrupts any set/dict using the instance as a key.
2. **`__init__` may merge rather than reset.** For dict, calling `dict.__init__(fd, {"new": 1})` MERGES into existing contents; it does NOT reset. Surprising relative to a fresh-object invocation. Document this in the probe output to avoid confusion.
3. **Only applies to INHERITANCE-based wrappers.** If the "frozen" class uses composition (`self._d = dict(...)`) instead of subclassing dict, the entire technique is inapplicable. Check `issubclass(FrozenClass, MutableBase)` first; if false, skip.
4. **`isinstance(fd, dict)` being True is related and often compounds.** An inheritance-based frozen dict reports True for `isinstance(fd, dict)`, which leads callers to use `dict.*` methods defensively — unknowingly taking the bypass route. Note this in any bug report.
5. **The fix is architectural** (switch from inheritance to composition), not per-method patching. `__setitem__` being overridden correctly can't save you; the base class has ~14 other ways in.
6. **Run against a pure-Python probe, not a C probe.** C-implemented frozen classes typically set `tp_base = 0` (not dict-derived), so the `dict.__setitem__(c_fd, ...)` route gets `TypeError: descriptor requires a 'dict' object but received 'frozendict'`. This technique is specifically valuable for pure-Python fallback classes.

### When to use this

- **Every pure-Python "frozen"/"readonly"/"immutable" wrapper** that subclasses a mutable type. Should be part of the test suite for any such library.
- **Before shipping a fallback** when the primary implementation is in C. The C side may be correctly immutable (fresh type, no dict inheritance), and the fallback may silently not be.
- **During security-review** of any library advertising immutability. The enumeration is mechanical; the bypasses are concrete; the output is an explicit count.

**Confirmed on**:
- frozendict 2.4.7 pure-Python fallback (`src/frozendict/_frozendict_py.py`, `class frozendict(dict)`) — 14 distinct mutation-bypass routes. Every user on Python 3.11+ (where no C source tree exists) hits this because the pure-Python fallback is inherited from dict. Reproducer: `reports/frozendict_v1/reproducers/repro_f4_parity_dict_setitem.py`. Observed: all 14 routes mutate a fresh `frozendict({"a": 1, "b": 2})`, including `dict.__setitem__`, `dict.update` (3 signatures), `dict.clear`, `super().__init__`, and a partial `dict.__init__(fd, ...)` that merges.

