# NumPy C Extension Report — Fix Suggestions Appendix

Companion to `numpy_report.md`. Contains fix suggestions for confirmed findings, showing current code and corrected code.

---

## Finding 1-3: Use-After-Free in Ufunc Dispatch (dispatching.cpp, wrapping_array_method.c)

All three findings share the same pattern: `PyList_GetItemRef` returns a new reference to the list item, `PyTuple_GetItem` borrows a sub-element, then `Py_DECREF(item)` destroys the container while the borrowed sub-element is still in use.

### Fix for Finding 1 — `dispatching.cpp:127-130`

**Current code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        Py_DECREF(item);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple, DType_tuple, Py_EQ);
```

**Fixed code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple, DType_tuple, Py_EQ);
        Py_DECREF(item);
```

### Fix for Finding 2 — `dispatching.cpp:1348-1352`

**Current code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        Py_DECREF(item);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple,
                                           t_dtypes, Py_EQ);
```

**Fixed code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple,
                                           t_dtypes, Py_EQ);
        Py_DECREF(item);
```

*Note: also move `Py_DECREF(item)` after the `cmp < 0` and `cmp == 0` checks, or at the end of each exit path.*

### Fix for Finding 3 — `wrapping_array_method.c:255-258`

**Current code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        Py_DECREF(item);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple, wrapped_dt_tuple, Py_EQ);
```

**Fixed code:**
```c
        PyObject *item = PyList_GetItemRef(loops, i);
        PyObject *cur_DType_tuple = PyTuple_GetItem(item, 0);
        int cmp = PyObject_RichCompareBool(cur_DType_tuple, wrapped_dt_tuple, Py_EQ);
        Py_DECREF(item);
```

*Note: line 265 also uses `item` after the DECREF via `PyTuple_GET_ITEM(item, 1)`. The `Py_DECREF(item)` must move to after all uses of both `cur_DType_tuple` and the line-265 access. A clean approach is to DECREF at each exit point (goto finish, continue, and fall-through).*

---

## Finding 4: DECREF on Borrowed Reference — `usertypes.c:364`

**Current code:**
```c
    /* cast_impl was fetched via PyDict_GetItemWithError (borrowed ref) */
    Py_DECREF(cast_impl);
```

**Fixed code — remove the DECREF entirely:**
```c
    /* cast_impl is a borrowed reference from PyDict_GetItemWithError — do not DECREF */
    /* Py_DECREF(cast_impl);  REMOVED — was a borrowed ref */
```

---

## Findings 5-6: User-Triggerable Crashes from Non-ASCII Input

### Fix for Finding 5 — `convert.c:301-306`

**Current code:**
```c
            byteobj = PyUnicode_AsASCIIString(strobj);
            NPY_BEGIN_ALLOW_THREADS;
            n2 = PyBytes_GET_SIZE(byteobj);
            n = fwrite(PyBytes_AS_STRING(byteobj), 1, n2, fp);
            NPY_END_ALLOW_THREADS;
            Py_DECREF(byteobj);
```

**Fixed code:**
```c
            byteobj = PyUnicode_AsASCIIString(strobj);
            if (byteobj == NULL) {
                Py_DECREF(strobj);
                Py_DECREF(it);
                return -1;
            }
            NPY_BEGIN_ALLOW_THREADS;
            n2 = PyBytes_GET_SIZE(byteobj);
            n = fwrite(PyBytes_AS_STRING(byteobj), 1, n2, fp);
            NPY_END_ALLOW_THREADS;
            Py_DECREF(byteobj);
```

### Fix for Finding 6 — `flagsobject.c:588-594`

**Current code:**
```c
    if (PyUnicode_Check(ind)) {
        PyObject *tmp_str;
        tmp_str = PyUnicode_AsASCIIString(ind);
        key = PyBytes_AS_STRING(tmp_str);
        n = PyBytes_GET_SIZE(tmp_str);
```

**Fixed code:**
```c
    if (PyUnicode_Check(ind)) {
        PyObject *tmp_str;
        tmp_str = PyUnicode_AsASCIIString(ind);
        if (tmp_str == NULL) {
            return -1;
        }
        key = PyBytes_AS_STRING(tmp_str);
        n = PyBytes_GET_SIZE(tmp_str);
```

---

## Findings 7-8: Timsort Heap Corruption — `timsort.cpp:79,1821`

Both variants have the same bug: `buffer->size` is updated before checking if `realloc` returned NULL. On failure, the old pointer (`buffer->pw`) is freed by `realloc`, `buffer->size` is wrong, and subsequent code uses the stale pointer.

### Fix for Finding 7 — `timsort.cpp:73-89`

**Current code:**
```c
static inline int
resize_buffer_intp(buffer_intp *buffer, npy_intp new_size)
{
    if (new_size <= buffer->size) {
        return 0;
    }

    npy_intp *new_pw = (npy_intp *)realloc(buffer->pw, new_size * sizeof(npy_intp));

    buffer->size = new_size;

    if (NPY_UNLIKELY(new_pw == NULL)) {
        return -NPY_ENOMEM;
    }
    else {
        buffer->pw = new_pw;
        return 0;
    }
```

**Fixed code:**
```c
static inline int
resize_buffer_intp(buffer_intp *buffer, npy_intp new_size)
{
    if (new_size <= buffer->size) {
        return 0;
    }

    npy_intp *new_pw = (npy_intp *)realloc(buffer->pw, new_size * sizeof(npy_intp));

    if (NPY_UNLIKELY(new_pw == NULL)) {
        return -NPY_ENOMEM;
    }

    buffer->pw = new_pw;
    buffer->size = new_size;
    return 0;
```

### Fix for Finding 8 — `timsort.cpp:1815-1829`

**Current code:**
```c
    char *new_pw = (char *)realloc(buffer->pw, sizeof(char) * new_size * buffer->len);
    buffer->size = new_size;

    if (NPY_UNLIKELY(new_pw == NULL)) {
        return -NPY_ENOMEM;
    }
    else {
        buffer->pw = new_pw;
        return 0;
```

**Fixed code:**
```c
    char *new_pw = (char *)realloc(buffer->pw, sizeof(char) * new_size * buffer->len);

    if (NPY_UNLIKELY(new_pw == NULL)) {
        return -NPY_ENOMEM;
    }

    buffer->pw = new_pw;
    buffer->size = new_size;
    return 0;
```

---

## Finding 9: NULL Deref in `fill_zero_object_strided_loop` — `dtype_traversal.c:178`

**Current code:**
```c
    PyObject *zero = PyLong_FromLong(0);
    while (size--) {
        Py_INCREF(zero);
```

**Fixed code:**
```c
    PyObject *zero = PyLong_FromLong(0);
    if (zero == NULL) {
        return -1;
    }
    while (size--) {
        Py_INCREF(zero);
```

*Note: `PyLong_FromLong(0)` uses the small-int cache and practically never fails, but the NULL check is required for correctness.*

---

## Finding 10: NULL Deref in `PyArray_Arange` — `ctors.c:3206`

**Current code:**
```c
    obj = PyFloat_FromDouble(start);
    ret = funcs->setitem(obj, PyArray_DATA(range), range);
    Py_DECREF(obj);
```

**Fixed code:**
```c
    obj = PyFloat_FromDouble(start);
    if (obj == NULL) {
        goto fail;
    }
    ret = funcs->setitem(obj, PyArray_DATA(range), range);
    Py_DECREF(obj);
```

*Same fix needed at line 3215:*

**Current:**
```c
    obj = PyFloat_FromDouble(start + step);
```

**Fixed:**
```c
    obj = PyFloat_FromDouble(start + step);
    if (obj == NULL) {
        goto fail;
    }
```

---

## Finding 11: NULL Deref in `PyArray_AssignZero` — `convert.c:477`

**Current code:**
```c
    if (PyArray_ISOBJECT(dst)) {
        PyObject * pZero = PyLong_FromLong(0);
        retcode = PyArray_AssignRawScalar(dst, PyArray_DESCR(dst),
                                     (char *)&pZero, wheremask, NPY_SAFE_CASTING);
        Py_DECREF(pZero);
```

**Fixed code:**
```c
    if (PyArray_ISOBJECT(dst)) {
        PyObject * pZero = PyLong_FromLong(0);
        if (pZero == NULL) {
            return -1;
        }
        retcode = PyArray_AssignRawScalar(dst, PyArray_DESCR(dst),
                                     (char *)&pZero, wheremask, NPY_SAFE_CASTING);
        Py_DECREF(pZero);
```

---

## Finding 12: NULL Deref in `npyiter_multi_index_set` — `nditer_pywrap.c:1678`

**Current code:**
```c
        for (idim = 0; idim < ndim; ++idim) {
            PyObject *v = PySequence_GetItem(value, idim);
            multi_index[idim] = PyLong_AsLong(v);
            Py_DECREF(v);
```

**Fixed code:**
```c
        for (idim = 0; idim < ndim; ++idim) {
            PyObject *v = PySequence_GetItem(value, idim);
            if (v == NULL) {
                return -1;
            }
            multi_index[idim] = PyLong_AsLong(v);
            Py_DECREF(v);
```

---

## Finding 13: NULL Deref in `_convert_from_array_descr` — `descriptor.c:485`

**Current code:**
```c
        else if (PyTuple_GET_SIZE(item) == 3) {
            PyObject *newobj = PyTuple_GetSlice(item, 1, 3);
            conv = _convert_from_any(newobj, align);
            Py_DECREF(newobj);
```

**Fixed code:**
```c
        else if (PyTuple_GET_SIZE(item) == 3) {
            PyObject *newobj = PyTuple_GetSlice(item, 1, 3);
            if (newobj == NULL) {
                goto fail;
            }
            conv = _convert_from_any(newobj, align);
            Py_DECREF(newobj);
```

---

## Findings 14-15: NULL Deref in `_convert_from_dict` — `descriptor.c:1110,1121`

**Current code (line 1110):**
```c
        PyObject *ind = PyLong_FromLong(i);
```

**Fixed code:**
```c
        PyObject *ind = PyLong_FromLong(i);
        if (ind == NULL) {
            goto fail;
        }
```

**Current code (line 1121):**
```c
        PyObject *tup = PyTuple_New(len);
```

**Fixed code:**
```c
        PyObject *tup = PyTuple_New(len);
        if (tup == NULL) {
            Py_DECREF(ind);
            goto fail;
        }
```

---

## Finding 16: NULL Deref in `dtypemeta_ensure_canonical` — `dtypemeta.c:721`

**Current code:**
```c
            PyObject *new_tuple = PyTuple_New(PyTuple_GET_SIZE(tuple));
            PyArray_Descr *field_descr = NPY_DT_CALL_ensure_canonical(
                    (PyArray_Descr *)PyTuple_GET_ITEM(tuple, 0));
            if (field_descr == NULL) {
                Py_DECREF(new_tuple);
```

**Fixed code:**
```c
            PyObject *new_tuple = PyTuple_New(PyTuple_GET_SIZE(tuple));
            if (new_tuple == NULL) {
                Py_DECREF(new);
                return NULL;
            }
            PyArray_Descr *field_descr = NPY_DT_CALL_ensure_canonical(
                    (PyArray_Descr *)PyTuple_GET_ITEM(tuple, 0));
            if (field_descr == NULL) {
                Py_DECREF(new_tuple);
```

---

## Finding 17: NULL Deref in `_buffer_format_string` — `buffer.c:221`

**Current code:**
```c
        else {
            subarray_tuple = Py_BuildValue("(O)", ldescr->subarray->shape);
        }

        if (_append_char(str, '(') < 0) {
```

**Fixed code:**
```c
        else {
            subarray_tuple = Py_BuildValue("(O)", ldescr->subarray->shape);
            if (subarray_tuple == NULL) {
                return -1;
            }
        }

        if (_append_char(str, '(') < 0) {
```

---

## Finding 18: NULL Deref in `_descriptor_builtin_hash` — `hashdescr.c:83`

**Current code:**
```c
    t = Py_BuildValue("(ccKnn)", descr->kind, nbyteorder,
            descr->flags, descr->elsize, descr->alignment);

    for(i = 0; i < PyTuple_Size(t); ++i) {
```

**Fixed code:**
```c
    t = Py_BuildValue("(ccKnn)", descr->kind, nbyteorder,
            descr->flags, descr->elsize, descr->alignment);
    if (t == NULL) {
        return -1;
    }

    for(i = 0; i < PyTuple_Size(t); ++i) {
```

---

## Finding 19: NULL Deref in `PyArray_GetCastFunc` — `convert_datatype.c:353`

**Current code:**
```c
            key = PyLong_FromLong(type_num);
            cobj = PyDict_GetItem(obj, key); // noqa: borrowed-ref OK
            Py_DECREF(key);
```

**Fixed code:**
```c
            key = PyLong_FromLong(type_num);
            if (key == NULL) {
                return NULL;
            }
            cobj = PyDict_GetItem(obj, key);
            Py_DECREF(key);
```

---

## Finding 20: NULL Deref in `array_reduce` — `methods.c:1798-1807`

**Current code:**
```c
    obj = PyObject_GetAttrString(mod, "_reconstruct");
    Py_DECREF(mod);
    PyTuple_SET_ITEM(ret, 0, obj);
    PyTuple_SET_ITEM(ret, 1,
                     Py_BuildValue("ONc",
                                   (PyObject *)Py_TYPE(self),
                                   Py_BuildValue("(N)",
                                                 PyLong_FromLong(0)),
                                   /* dummy data-type */
                                   'b'));
```

**Fixed code:**
```c
    obj = PyObject_GetAttrString(mod, "_reconstruct");
    Py_DECREF(mod);
    if (obj == NULL) {
        Py_DECREF(ret);
        return NULL;
    }
    PyTuple_SET_ITEM(ret, 0, obj);
    PyObject *inner = PyLong_FromLong(0);
    if (inner == NULL) {
        Py_DECREF(ret);
        return NULL;
    }
    PyObject *shape_tup = Py_BuildValue("(N)", inner);
    if (shape_tup == NULL) {
        Py_DECREF(ret);
        return NULL;
    }
    PyObject *item1 = Py_BuildValue("ONc",
                                    (PyObject *)Py_TYPE(self),
                                    shape_tup,
                                    'b');
    if (item1 == NULL) {
        Py_DECREF(ret);
        return NULL;
    }
    PyTuple_SET_ITEM(ret, 1, item1);
```

*Same pattern for `state` tuple (lines 1830-1831):*

**Current:**
```c
    PyTuple_SET_ITEM(state, 0, PyLong_FromLong(version));
    PyTuple_SET_ITEM(state, 1, PyObject_GetAttrString((PyObject *)self, "shape"));
```

**Fixed:**
```c
    PyObject *ver_obj = PyLong_FromLong(version);
    if (ver_obj == NULL) {
        Py_DECREF(state);
        Py_DECREF(ret);
        return NULL;
    }
    PyTuple_SET_ITEM(state, 0, ver_obj);
    PyObject *shape_obj = PyObject_GetAttrString((PyObject *)self, "shape");
    if (shape_obj == NULL) {
        Py_DECREF(state);
        Py_DECREF(ret);
        return NULL;
    }
    PyTuple_SET_ITEM(state, 1, shape_obj);
```

---

## Finding 21: NULL Deref in `dtype_transfer.c:171` — `_any_to_object_auxdata_clone`

**Current code:**
```c
    _any_to_object_auxdata *res = PyMem_Malloc(sizeof(_any_to_object_auxdata));

    res->base = data->base;
```

**Fixed code:**
```c
    _any_to_object_auxdata *res = PyMem_Malloc(sizeof(_any_to_object_auxdata));
    if (res == NULL) {
        PyErr_NoMemory();
        return NULL;
    }

    res->base = data->base;
```

---

## Finding 22: NULL Deref in `NpyIter_Copy` — `nditer_constr.c:540`

**Current code:**
```c
    newiter = (NpyIter*)PyObject_Malloc(size);

    /* Copy the raw values to the new iterator */
    memcpy(newiter, iter, size);
```

**Fixed code:**
```c
    newiter = (NpyIter*)PyObject_Malloc(size);
    if (newiter == NULL) {
        PyErr_NoMemory();
        return NULL;
    }

    /* Copy the raw values to the new iterator */
    memcpy(newiter, iter, size);
```

---

## Finding 22 (from report): `OBJECT_dot` Empty-Vector Bug — `arraytypes.c.src:3638`

The same bug that was fixed in `OBJECT_dotc` (commit 5033aa9). When `n == 0`, the loop never executes, `tmp` stays NULL, and NULL is written to `*op`.

**Current code:**
```c
NPY_NO_EXPORT void
OBJECT_dot(char *ip1, npy_intp is1, char *ip2, npy_intp is2, char *op, npy_intp n,
           void *NPY_UNUSED(ignore))
{
    npy_intp i;
    PyObject *tmp1, *tmp2, *tmp = NULL;
    PyObject **tmp3;
    for (i = 0; i < n; i++, ip1 += is1, ip2 += is2) {
```

**Fixed code (add before the loop, matching `OBJECT_dotc:717-724`):**
```c
NPY_NO_EXPORT void
OBJECT_dot(char *ip1, npy_intp is1, char *ip2, npy_intp is2, char *op, npy_intp n,
           void *NPY_UNUSED(ignore))
{
    npy_intp i;
    PyObject *tmp1, *tmp2, *tmp = NULL;
    PyObject **tmp3;

    if (n == 0) {
        PyObject *zero = PyLong_FromLong(0);
        if (zero == NULL) {
            return;
        }
        Py_XSETREF(*((PyObject **)op), zero);
        return;
    }

    for (i = 0; i < n; i++, ip1 += is1, ip2 += is2) {
```

---

## Finding 23: `sfloat_get_ufunc` NULL Deref — `_scaled_float_dtype.c:703-705`

**Current code:**
```c
    PyObject *ufunc = PyObject_GetAttrString(mod, ufunc_name);
    Py_DECREF(mod);
    if (!PyObject_TypeCheck(ufunc, &PyUFunc_Type)) {
```

**Fixed code:**
```c
    PyObject *ufunc = PyObject_GetAttrString(mod, ufunc_name);
    Py_DECREF(mod);
    if (ufunc == NULL) {
        return NULL;
    }
    if (!PyObject_TypeCheck(ufunc, &PyUFunc_Type)) {
```

---

## Findings 24-28: `PyModule_AddObject` Leaks — `umathmodule.c:217-221`

**Current code:**
```c
    PyModule_AddObject(m, "PINF", PyFloat_FromDouble(NPY_INFINITY));
    PyModule_AddObject(m, "NINF", PyFloat_FromDouble(-NPY_INFINITY));
    PyModule_AddObject(m, "PZERO", PyFloat_FromDouble(NPY_PZERO));
    PyModule_AddObject(m, "NZERO", PyFloat_FromDouble(NPY_NZERO));
    PyModule_AddObject(m, "NAN", PyFloat_FromDouble(NPY_NAN));
```

**Fixed code — use `PyModule_AddObjectRef` (steals no reference):**
```c
    PyObject *tmp;

    tmp = PyFloat_FromDouble(NPY_INFINITY);
    if (PyModule_AddObjectRef(m, "PINF", tmp) < 0) { Py_XDECREF(tmp); return -1; }
    Py_DECREF(tmp);

    tmp = PyFloat_FromDouble(-NPY_INFINITY);
    if (PyModule_AddObjectRef(m, "NINF", tmp) < 0) { Py_XDECREF(tmp); return -1; }
    Py_DECREF(tmp);

    tmp = PyFloat_FromDouble(NPY_PZERO);
    if (PyModule_AddObjectRef(m, "PZERO", tmp) < 0) { Py_XDECREF(tmp); return -1; }
    Py_DECREF(tmp);

    tmp = PyFloat_FromDouble(NPY_NZERO);
    if (PyModule_AddObjectRef(m, "NZERO", tmp) < 0) { Py_XDECREF(tmp); return -1; }
    Py_DECREF(tmp);

    tmp = PyFloat_FromDouble(NPY_NAN);
    if (PyModule_AddObjectRef(m, "NAN", tmp) < 0) { Py_XDECREF(tmp); return -1; }
    Py_DECREF(tmp);
```

*Alternative (simpler, if using Python 3.10+):*
```c
    if (PyModule_AddObjectRef(m, "PINF", PyFloat_FromDouble(NPY_INFINITY)) < 0) return -1;
    if (PyModule_AddObjectRef(m, "NINF", PyFloat_FromDouble(-NPY_INFINITY)) < 0) return -1;
    if (PyModule_AddObjectRef(m, "PZERO", PyFloat_FromDouble(NPY_PZERO)) < 0) return -1;
    if (PyModule_AddObjectRef(m, "NZERO", PyFloat_FromDouble(NPY_NZERO)) < 0) return -1;
    if (PyModule_AddObjectRef(m, "NAN", PyFloat_FromDouble(NPY_NAN)) < 0) return -1;
```

*Note: This alternative still leaks the PyFloat on AddObjectRef failure. The first version is fully correct.*

---

## Finding 29: Borrowed Refs Across Dict Modification — `umathmodule.c:223-235`

**Current code:**
```c
    s = PyDict_GetItemString(d, "divide"); // noqa: borrowed-ref OK
    PyDict_SetItemString(d, "true_divide", s);

    s = PyDict_GetItemString(d, "conjugate"); // noqa: borrowed-ref OK
    s2 = PyDict_GetItemString(d, "remainder"); // noqa: borrowed-ref OK

    if (_PyArray_SetNumericOps(d) < 0) {
        return -1;
    }

    PyDict_SetItemString(d, "conj", s);
    PyDict_SetItemString(d, "mod", s2);
```

**Fixed code:**
```c
    s = PyDict_GetItemString(d, "divide");
    Py_XINCREF(s);
    PyDict_SetItemString(d, "true_divide", s);

    s2 = PyDict_GetItemString(d, "conjugate");
    Py_XINCREF(s2);
    PyObject *s3 = PyDict_GetItemString(d, "remainder");
    Py_XINCREF(s3);

    if (_PyArray_SetNumericOps(d) < 0) {
        Py_XDECREF(s);
        Py_XDECREF(s2);
        Py_XDECREF(s3);
        return -1;
    }

    PyDict_SetItemString(d, "conj", s2);
    PyDict_SetItemString(d, "mod", s3);
    Py_XDECREF(s);
    Py_XDECREF(s2);
    Py_XDECREF(s3);
```

---

## Finding 30: StringDType Hash NULL Deref — `stringdtype/dtype.c:911`

**Current code:**
```c
    if (sself->na_object != NULL) {
        hash_tup = Py_BuildValue("(iO)", sself->coerce, sself->na_object);
    }
    else {
        hash_tup = Py_BuildValue("(i)", sself->coerce);
    }

    Py_hash_t ret = PyObject_Hash(hash_tup);
    Py_DECREF(hash_tup);
    return ret;
```

**Fixed code:**
```c
    if (sself->na_object != NULL) {
        hash_tup = Py_BuildValue("(iO)", sself->coerce, sself->na_object);
    }
    else {
        hash_tup = Py_BuildValue("(i)", sself->coerce);
    }
    if (hash_tup == NULL) {
        return -1;
    }

    Py_hash_t ret = PyObject_Hash(hash_tup);
    Py_DECREF(hash_tup);
    return ret;
```

---

## Summary Table

| Finding | File | Fix Type | Effort |
|---------|------|----------|--------|
| 1-3 | dispatching.cpp, wrapping_array_method.c | Move DECREF after use | Small (3 sites) |
| 4 | usertypes.c:364 | Remove erroneous DECREF | 1 line |
| 5 | convert.c:301 | Add NULL check | 4 lines |
| 6 | flagsobject.c:588 | Add NULL check | 3 lines |
| 7-8 | timsort.cpp:79,1821 | Move size update after NULL check | 2 lines x2 |
| 9 | dtype_traversal.c:178 | Add NULL check | 3 lines |
| 10 | ctors.c:3206,3215 | Add NULL check | 3 lines x2 |
| 11 | convert.c:477 | Add NULL check | 3 lines |
| 12 | nditer_pywrap.c:1678 | Add NULL check | 3 lines |
| 13 | descriptor.c:485 | Add NULL check | 3 lines |
| 14-15 | descriptor.c:1110,1121 | Add NULL checks | 3 lines x2 |
| 16 | dtypemeta.c:721 | Add NULL check | 4 lines |
| 17 | buffer.c:221 | Add NULL check | 3 lines |
| 18 | hashdescr.c:83 | Add NULL check | 3 lines |
| 19 | convert_datatype.c:353 | Add NULL check | 3 lines |
| 20 | methods.c:1798-1831 | Add NULL checks for nested Py_BuildValue | ~20 lines |
| 21 | dtype_transfer.c:171 | Add NULL check | 3 lines |
| 22 (nditer) | nditer_constr.c:540 | Add NULL check | 3 lines |
| 22 (OBJECT_dot) | arraytypes.c.src:3638 | Add n==0 guard | 7 lines |
| 23 | _scaled_float_dtype.c:703 | Add NULL check | 3 lines |
| 24-28 | umathmodule.c:217-221 | Migrate to PyModule_AddObjectRef | 15 lines |
| 29 | umathmodule.c:223-235 | INCREF borrowed refs before dict mutation | 10 lines |
| 30 | stringdtype/dtype.c:911 | Add NULL check | 3 lines |
