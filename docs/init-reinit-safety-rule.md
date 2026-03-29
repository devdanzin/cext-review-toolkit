# tp_init Re-Init Safety and tp_new Uninitialized State Rule

Implementation guide for adding these checks to cpython-review-toolkit (and any other review toolkit). Based on APSW maintainer Roger Binns' suggestion.

## Background

Python allows two calling patterns that are impossible in C++:

1. **Multiple `tp_init` calls**: `obj.__init__()` can be called again after construction. If `tp_init` allocates resources (malloc, PyList_New, fopen, etc.) without first cleaning up existing state, the second call leaks or double-frees the first call's resources.

2. **`tp_new` without `tp_init`**: `object.__new__(MyType)` creates an instance without calling `__init__`. If `tp_new` doesn't initialize pointer members to safe defaults (NULL/0), methods may dereference uninitialized garbage.

These are real, underappreciated bug classes. Most C extension authors think of `__init__` as a constructor (one-shot), but Python's object model does not enforce this.

## Rule 1: `init_not_reinit_safe`

### What to detect

A `tp_init` function that:
1. Allocates resources (calls allocation functions)
2. Assigns to pointer struct members (`self->field = ...`)
3. Does NOT have a re-init guard

### Allocation patterns to look for

```c
PyMem_Malloc, PyMem_Calloc, PyMem_Realloc,
malloc, calloc, realloc,
PyObject_New, PyObject_GC_New,
PyList_New, PyDict_New, PyTuple_New, PySet_New,
PyUnicode_FromString, PyBytes_FromString,
Py_BuildValue, PyObject_Call,
fopen, open  // file handles
```

### Re-init guard patterns to recognize (NOT a bug)

These patterns indicate the developer has handled re-init:

```c
// Pattern 1: "already initialized" error message
"already initialized"
"already_initialized"
"cannot reinitialize"

// Pattern 2: Macro-based guards (e.g., APSW)
PREVENT_INIT_MULTIPLE_CALLS
PREVENT_INIT

// Pattern 3: Flag-based guards
init_was_called
initialized

// Pattern 4: Cleanup before re-init
if (self->member != NULL) { Py_CLEAR(self->member); }
if (self->member != NULL) { Py_XDECREF(self->member); }
if (self->member != NULL) { Py_DECREF(self->member); }
if (self->member != NULL) { free(self->member); }
if (self->member != NULL) { PyMem_Free(self->member); }
```

### Classification

- **FIX** if `tp_init` allocates AND assigns to pointer members without any guard
- **ACCEPTABLE** if any guard pattern is present

### Example: vulnerable tp_init

```c
static int
MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)
{
    // BUG: no check for prior initialization
    self->data = PyList_New(0);       // leaks previous self->data
    self->buffer = PyMem_Malloc(1024); // leaks previous self->buffer
    return 0;
}
```

### Example: safe tp_init (reject re-init)

```c
static int
MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)
{
    if (self->initialized) {
        PyErr_SetString(PyExc_RuntimeError,
                        "__init__ has already been called");
        return -1;
    }
    self->data = PyList_New(0);
    self->buffer = PyMem_Malloc(1024);
    self->initialized = 1;
    return 0;
}
```

### Example: safe tp_init (clean up first)

```c
static int
MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)
{
    // Clean up prior state before re-initializing
    if (self->data != NULL) {
        Py_CLEAR(self->data);
    }
    if (self->buffer != NULL) {
        PyMem_Free(self->buffer);
        self->buffer = NULL;
    }
    self->data = PyList_New(0);
    self->buffer = PyMem_Malloc(1024);
    return 0;
}
```

## Rule 2: `new_missing_member_init`

### What to detect

A `tp_new` function that:
1. Uses a NON-zeroing allocator
2. Does NOT initialize all pointer struct members to NULL/safe defaults

### Zeroing allocators (NOT a bug — these zero all fields)

```c
type->tp_alloc(type, 0)     // tp_alloc zeros memory
PyType_GenericAlloc(type, 0) // zeros memory
calloc(1, sizeof(MyObj))     // zeros memory
memset(self, 0, sizeof(*self)) // explicit zero
```

### Non-zeroing allocators (need individual member init)

```c
PyObject_New(MyObj, type)    // does NOT zero custom fields
PyObject_GC_New(MyObj, type) // does NOT zero custom fields
malloc(sizeof(MyObj))         // does NOT zero memory
```

### Classification

- **CONSIDER** if `tp_new` uses a non-zeroing allocator and doesn't initialize pointer members
- **ACCEPTABLE** if a zeroing allocator is used, or if all pointer members are explicitly set

### Example: vulnerable tp_new

```c
static PyObject *
MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    MyObj *self = (MyObj *)PyObject_New(MyObj, type);
    // BUG: self->data and self->buffer are uninitialized garbage
    // If __new__() is called without __init__(), methods will
    // dereference garbage pointers
    return (PyObject *)self;
}
```

### Example: safe tp_new (zeroing allocator)

```c
static PyObject *
MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    MyObj *self = (MyObj *)type->tp_alloc(type, 0);
    // tp_alloc zeros all memory — safe even without __init__()
    return (PyObject *)self;
}
```

### Example: safe tp_new (explicit init)

```c
static PyObject *
MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    MyObj *self = (MyObj *)PyObject_New(MyObj, type);
    if (self != NULL) {
        self->data = NULL;
        self->buffer = NULL;
        self->count = 0;
    }
    return (PyObject *)self;
}
```

## CPython-specific considerations

For cpython-review-toolkit, the same checks apply but with some differences:

1. **CPython types typically use `PyType_GenericAlloc`** for `tp_alloc`, which zeros memory. This means `new_missing_member_init` findings will be less common in CPython itself.

2. **CPython types often use `PyType_GenericNew`** for `tp_new`, which delegates to `tp_alloc`. These are safe by default.

3. **CPython's own types sometimes rely on `tp_alloc` zeroing** and don't explicitly initialize members in `tp_init`. This is correct but fragile — adding a member later requires remembering that `tp_alloc` zeroes it.

4. **For the reinit check in CPython**: look at types in `Modules/`, `Objects/`, and `Python/`. Most built-in types reject re-init or are immutable. Third-party modules in `Modules/` are the most likely to have this bug.

5. **The scanner needs to handle CPython's macro conventions**: `Py_VISIT`, `Py_CLEAR`, and the `clinic` generated code that wraps `tp_init`.

## Implementation reference

See the cext-review-toolkit implementation:
- Scanner: `plugins/cext-review-toolkit/scripts/scan_type_slots.py` — functions `_check_init_reinit_safety` and `_check_new_without_init`
- Agent prompt: `plugins/cext-review-toolkit/agents/type-slot-checker.md` — "tp_new / tp_init review" section
- Tests: `tests/test_scan_type_slots.py` — `TestInitReinitSafety` and `TestNewWithoutInit` classes

The cext-review-toolkit implementation uses tree-sitter for parsing and regex for pattern matching within function bodies. The cpython-review-toolkit should follow the same approach, adapted for its own scanner infrastructure.
