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

