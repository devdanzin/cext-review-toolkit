# cereggii Report — Appendix: Suggested Fixes

## Fix A: Critical section escape via `goto` (Finding 1)

**Pattern:** `goto fail` jumps past `Py_END_CRITICAL_SECTION()`, leaving the per-object mutex locked on free-threaded Python.

**Before** (`lookup.c:207-277`):
```c
Py_BEGIN_CRITICAL_SECTION(batch);

    // ... loop body ...
    hash = PyObject_Hash(key);
    if (hash == -1)
        goto fail;          // <-- skips Py_END_CRITICAL_SECTION!
    // ...
    if (PyDict_SetItem(batch, key, result.entry.value) < 0)
        goto fail;          // <-- skips Py_END_CRITICAL_SECTION!

Py_END_CRITICAL_SECTION();
// ...
return batch;
fail:
    return NULL;
```

**After:**
```c
Py_BEGIN_CRITICAL_SECTION(batch);

    // ... loop body ...
    hash = PyObject_Hash(key);
    if (hash == -1)
        goto fail_in_cs;
    // ...
    if (PyDict_SetItem(batch, key, result.entry.value) < 0)
        goto fail_in_cs;

Py_END_CRITICAL_SECTION();
// ...
return batch;
fail_in_cs:
    Py_END_CRITICAL_SECTION();
fail:
    // existing cleanup
    return NULL;
```

**Applies to:** `lookup.c:207-268` (`BatchGetItem`), `atomic_dict.c:236-260` (`AtomicDict_init` — same pattern with `goto fail` and `goto create`).

---

## Fix B: Leaked `Get_callable` return in binary/unary/comparison ops (Findings 2, 3)

**Pattern:** `AtomicInt64_Get_callable()` returns a new reference that is passed to a `PyNumber_*` function and never DECREF'd.

**Before** (`atomic_int.c:567-587`, macro):
```c
#define ATOMICINT64_BIN_OP(op) \
    PyObject * \
    AtomicInt64_##op(AtomicInt64 *self, PyObject *other) { \
        PyObject *current = NULL; \
        if (PyObject_IsInstance(other, (PyObject *) &AtomicInt64_Type)) { \
            other = AtomicInt64_Get_callable((AtomicInt64 *) other); \
            current = (PyObject *) self; \
        } else { \
            current = AtomicInt64_Get_callable(self); \
        } \
        if (current == NULL || other == NULL) \
            goto fail; \
        return PyNumber_##op(current, other); \
        fail: \
        return NULL; \
    }
```

**After:**
```c
#define ATOMICINT64_BIN_OP(op) \
    PyObject * \
    AtomicInt64_##op(AtomicInt64 *self, PyObject *other) { \
        PyObject *current = NULL; \
        PyObject *to_decref = NULL; \
        int is_inst = PyObject_IsInstance(other, (PyObject *) &AtomicInt64_Type); \
        if (is_inst < 0) \
            return NULL; \
        if (is_inst) { \
            to_decref = AtomicInt64_Get_callable((AtomicInt64 *) other); \
            other = to_decref; \
            current = (PyObject *) self; \
        } else { \
            to_decref = AtomicInt64_Get_callable(self); \
            current = to_decref; \
        } \
        if (current == NULL || other == NULL) { \
            Py_XDECREF(to_decref); \
            return NULL; \
        } \
        PyObject *result = PyNumber_##op(current, other); \
        Py_DECREF(to_decref); \
        return result; \
    }
```

**Same fix pattern for unary ops** (`atomic_int.c:617-731`):
```c
// Before:
return PyNumber_Negative(current);

// After:
PyObject *result = PyNumber_Negative(current);
Py_DECREF(current);
return result;
```

**Same fix pattern for RichCompare** (`atomic_int.c:1092-1106`):
```c
// Before:
return PyObject_RichCompare(current, other, op);

// After:
PyObject *result = PyObject_RichCompare(current, other, op);
Py_DECREF(current);
return result;
```

---

## Fix C: CAS retry loop leaking references (Finding 4)

**Pattern:** `PyLong_FromInt64` and `PyObject_CallOneArg` create new refs each iteration, overwriting previous ones without DECREF.

**Before** (`atomic_int.c:450-475`):
```c
do {
    current = AtomicInt64_Get(self);
    py_current = PyLong_FromInt64(current);
    if (py_current == NULL)
        goto fail;
    py_desired = PyObject_CallOneArg(callable, py_current);
    if (!AtomicInt64_ConvertToCLongOrSetException(py_desired, &desired))
        goto fail;
} while (!AtomicInt64_CompareAndSet(self, current, desired));
return current;
fail:
    *error = 1;
    return -1;
```

**After:**
```c
do {
    current = AtomicInt64_Get(self);
    Py_XDECREF(py_current);
    py_current = PyLong_FromInt64(current);
    if (py_current == NULL)
        goto fail;
    Py_XDECREF(py_desired);
    py_desired = PyObject_CallOneArg(callable, py_current);
    if (py_desired == NULL)
        goto fail;
    if (!AtomicInt64_ConvertToCLongOrSetException(py_desired, &desired))
        goto fail;
} while (!AtomicInt64_CompareAndSet(self, current, desired));
Py_DECREF(py_current);
Py_DECREF(py_desired);
return current;
fail:
    Py_XDECREF(py_current);
    Py_XDECREF(py_desired);
    *error = 1;
    return -1;
```

**Applies to:** `AtomicInt64_GetAndUpdate` (line 450) and `AtomicInt64_UpdateAndGet` (line 493).

---

## Fix D: Leaked `Py_BuildValue` result in `Py_BuildValue("(OO)", ...)` (Finding 5)

**Pattern:** `Py_BuildValue` with `"O"` format INCREFs the argument; the caller's own new reference is never DECREF'd.

**Before** (`atomic_dict.c:388-401`):
```c
PyObject *approx_len = AtomicDict_ApproxLen(self);
if (approx_len == NULL) {
    goto fail;
}
return Py_BuildValue("(OO)", approx_len, approx_len);
```

**After:**
```c
PyObject *approx_len = AtomicDict_ApproxLen(self);
if (approx_len == NULL) {
    goto fail;
}
PyObject *result = Py_BuildValue("(OO)", approx_len, approx_len);
Py_DECREF(approx_len);
return result;
```

---

## Fix E: Leaked original `len` + unchecked `PyNumber_InPlaceAdd` (Finding 6)

**Pattern:** `PyNumber_InPlaceAdd` on immutable `int` returns a new object. The original is overwritten without DECREF, and the result is not NULL-checked.

**Before** (`atomic_dict.c:482-483`):
```c
len = PyNumber_InPlaceAdd(len, added_since_clean);
len_ssize_t = PyLong_AsSsize_t(len);
```

**After:**
```c
PyObject *new_len = PyNumber_InPlaceAdd(len, added_since_clean);
Py_DECREF(len);
len = new_len;
if (len == NULL)
    goto fail;
len_ssize_t = PyLong_AsSsize_t(len);
```

---

## Fix F: Leaked `handle` on error path in `GetHandle` functions (Finding 7)

**Pattern:** `fail:` label cleans up `args` but not `handle`. Also, `Py_BuildValue` result not NULL-checked.

**Before** (`atomic_dict.c:612-632`):
```c
handle = (ThreadHandle *) ThreadHandle_new(&ThreadHandle_Type, NULL, NULL);
if (handle == NULL)
    goto fail;
args = Py_BuildValue("(O)", self);
if (ThreadHandle_init(handle, args, NULL) < 0)
    goto fail;
// ...
fail:
    Py_XDECREF(args);
    return NULL;
```

**After:**
```c
handle = (ThreadHandle *) ThreadHandle_new(&ThreadHandle_Type, NULL, NULL);
if (handle == NULL)
    goto fail;
args = Py_BuildValue("(O)", self);
if (args == NULL)
    goto fail;
if (ThreadHandle_init(handle, args, NULL) < 0)
    goto fail;
// ...
fail:
    Py_XDECREF(handle);
    Py_XDECREF(args);
    return NULL;
```

**Applies to:** `AtomicDict_GetHandle`, `AtomicRef_GetHandle`, `AtomicInt64_GetHandle`.

---

## Fix G: Leaked `PyUnicode_FromFormat` in `PyErr_SetObject` (Finding 8)

**Pattern:** `PyErr_SetObject` does NOT steal its second argument. The simplest fix is to use `PyErr_Format` instead.

**Before** (`atomic_int.c:40-46`):
```c
if (overflowed) {
    PyErr_SetObject(
        PyExc_OverflowError,
        PyUnicode_FromFormat("%ld + %ld > %ld == (2 ** 63) - 1 "
                             "or %ld + %ld < %ld", current, to_add, INT64_MAX, current, to_add, INT64_MIN)
    );
}
```

**After:**
```c
if (overflowed) {
    PyErr_Format(
        PyExc_OverflowError,
        "%ld + %ld > %ld == (2 ** 63) - 1 "
        "or %ld + %ld < %ld", current, to_add, INT64_MAX, current, to_add, INT64_MIN
    );
}
```

For the `Cereggii_ExpectationFailed` case (`insert.c:324-326`) where a custom exception class is needed:
```c
// Before:
PyObject *error = PyUnicode_FromFormat("self[%R] != %R", key, expected);
PyErr_SetObject(Cereggii_ExpectationFailed, error);
goto fail;

// After:
PyObject *error = PyUnicode_FromFormat("self[%R] != %R", key, expected);
PyErr_SetObject(Cereggii_ExpectationFailed, error);
Py_XDECREF(error);
goto fail;
```

**Applies to:** `AtomicInt64_AddOrSetOverflow`, `SubOrSetOverflow`, `MulOrSetOverflow`, `DivOrSetException`, `AtomicDict_CompareAndSet_callable`.

---

## Fix H: Nested `Py_BuildValue` leaks + spurious `Py_INCREF` in Debug (Finding 9)

**Before** (`atomic_dict.c:526-528`):
```c
metadata = Py_BuildValue("{sOsO}",
                         "log_size\0", Py_BuildValue("B", meta->log_size),
                         "greatest_allocated_page\0", Py_BuildValue("L", meta->greatest_allocated_page));
```

**After** (use `N` format which steals the reference):
```c
metadata = Py_BuildValue("{sNsN}",
                         "log_size", Py_BuildValue("B", meta->log_size),
                         "greatest_allocated_page", Py_BuildValue("L", meta->greatest_allocated_page));
```

**Before** (`atomic_dict.c:576-577`):
```c
entry_tuple = Py_BuildValue("(KBnOO)", entry_ix, flags, hash, key, value);
if (entry_tuple == NULL)
    goto fail;
Py_INCREF(key);    // <-- spurious, Py_BuildValue "O" already INCREF'd
Py_INCREF(value);  // <-- spurious
```

**After:**
```c
entry_tuple = Py_BuildValue("(KBnOO)", entry_ix, flags, hash, key, value);
if (entry_tuple == NULL)
    goto fail;
// Remove the two Py_INCREF lines — "O" format already handles it
```

---

## Fix I: Remove incorrect `Py_DECREF` on static types in module init (Finding 12)

**Before** (`cereggii.c:446-464`):
```c
if (PyModule_AddObjectRef(m, "AtomicDict", (PyObject *) &AtomicDict_Type) < 0)
    goto fail;
Py_DECREF(&AtomicDict_Type);  // wrong: static type, no owned ref

// ... repeated for AtomicEvent_Type, AtomicRef_Type, AtomicInt64_Type, ThreadHandle_Type
```

**After:**
```c
if (PyModule_AddObjectRef(m, "AtomicDict", (PyObject *) &AtomicDict_Type) < 0)
    goto fail;
// No Py_DECREF — static types are not dynamically allocated

// Keep the Py_DECREF for dynamically-created objects (NOT_FOUND, ANY, etc.)
// because CereggiiConstant_New returns a new reference that should be transferred
```

---

## Fix J: Missing `PyErr_NoMemory` after `PyMem_RawMalloc` failure (Finding 13)

**Pattern:** `PyMem_RawMalloc` (unlike `PyMem_Malloc`) does not set a Python exception on failure.

**Before** (`meta.c:79`):
```c
pages = PyMem_RawMalloc(n * sizeof(AtomicDictPage *));
if (pages == NULL)
    goto fail;  // returns -1 without exception set → SystemError
```

**After:**
```c
pages = PyMem_RawMalloc(n * sizeof(AtomicDictPage *));
if (pages == NULL) {
    PyErr_NoMemory();
    goto fail;
}
```

**Applies to:** `meta_init_pages` (line 79) and `meta_copy_pages` (line 117).

---

## Fix K: Unchecked `PyObject_IsTrue` return value (Finding 15)

**Pattern:** `PyObject_IsTrue` returns -1 on error, which is truthy in C, causing the error to be silently swallowed.

**Before** (`insert.c:627-632`):
```c
if (PyObject_IsTrue(current) && PyObject_IsTrue(new)) {
    Py_INCREF(Py_True);
    return Py_True;
}
Py_INCREF(Py_False);
return Py_False;
```

**After:**
```c
int c = PyObject_IsTrue(current);
if (c < 0) return NULL;
int n = PyObject_IsTrue(new);
if (n < 0) return NULL;
if (c && n)
    Py_RETURN_TRUE;
Py_RETURN_FALSE;
```

**Applies to:** `reduce_specialized_and` (line 627) and `reduce_specialized_or` (line 670).

---

## Summary of Fix Classes

| Fix | Class | Instances | Effort |
|-----|-------|-----------|--------|
| A | Critical section escape via `goto` | 2 functions | Low — add `fail_in_cs` label |
| B | Leaked `Get_callable` return | 18 binary + 6 unary + 1 compare = 25 | Medium — macro rewrite |
| C | CAS retry loop leak | 2 functions | Low — add `Py_XDECREF` before reassignment |
| D | `Py_BuildValue("O")` doesn't steal ref | 1 function | Trivial |
| E | `PyNumber_InPlaceAdd` overwrites without DECREF | 1 function | Trivial |
| F | Missing cleanup on `fail:` label | 3 functions | Trivial |
| G | `PyErr_SetObject` doesn't steal ref | 5 functions | Trivial — use `PyErr_Format` |
| H | Nested `Py_BuildValue` + spurious INCREF | 1 function | Low — use `N` format |
| I | `Py_DECREF` on static types | 5 lines to delete | Trivial |
| J | Missing `PyErr_NoMemory` | 2 functions | Trivial |
| K | Unchecked `PyObject_IsTrue` return | 2 functions | Low |