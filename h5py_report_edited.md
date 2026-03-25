# h5py C Extension Analysis Report

## Findings by Priority

### FIX

#### HDF5 Handle Leaks:

1. `check_compound_complex` leaks 4 HDF5 type handles per call.
`H5Tget_member_type()` results are used inline in a boolean expression and discarded without `H5Tclose`. Each call leaks 4 type handles.
- `_conv.templ.pyx:915-927`


2. `h5_vlen_string` never closed in `dset_rw_vlen_strings`
`H5Tcopy(H5T_C_S1)` result is missing from the finally block. Leaks one type handle per vlen string read/write.
- `_proxy.pyx:191-231`


3. `_npystrings_pack` missing try/finally
If `H5Diterate` raises, both the string allocator and HDF5 type handle leak. The matching `_npystrings_unpack` correctly uses try/finally.
- `_npystrings.pyx:100-121`


4. Space handle leak in `write_direct_chunk` on validation error
`H5Dget_space` called before the try block; if `len(offsets) != rank` raises, the space handle is never closed.
- `h5d.pyx:516-531`


5. Space handle + buffer leak in `get_chunk_info` on error
No try/finally, so any HDF5 error leaks both the space handle and the malloc'd offset buffer.
- `h5d.pyx:621-653`


#### Unchecked malloc (segfault on OOM):

6. `H5FD_fileobj_open` has unchecked malloc + immediate deref
`stdlib_malloc(sizeof(H5FD_fileobj_t))` result used on the next line without NULL check. Segfault on OOM during file-object open.
- `h5fd.pyx:132-135`

7. `h5i.get_name` has unchecked malloc
`malloc(sizeof(char)*(namelen+1))` passed to `H5Iget_name` without NULL check. Segfault when getting an HDF5 object name under OOM.
- `h5i.pyx:101`


8. `h5r.get_name` has unchecked malloc
Same pattern as `h5i.get_name`. Should use `emalloc` (which checks for NULL) instead of raw `malloc`.
- `h5r.pyx:141`


### CONSIDER


1. `build_fancy_hyperslab` leaks `space2` on exception. Recursive, can leak multiple handles
- `_selector.pyx:247-262`

2. `readinto` return value unchecked in VFD driver. Partial reads leave buffer uninitialized (data corruption)
- `h5fd.templ.pyx:156-168`

3. `temptype` leak in `make_reduced_type` loop iteration
- `_proxy.pyx:271-288`

4. `ObjectID._dealloc` doesn't zero `self.id` after `H5Idec_ref`
- `_objects.pyx:209-218`

5. `Reader` holds raw `hid_t` without HDF5 refcount increment
- `_selector.pyx:326-327`

6. Unbalanced `Py_INCREF(elem_dtype)` in vlen conversion, dtype ref never released
- `_conv.pyx:760`

7. Static `_error_handler` data race under free-threading (PEP 703)
- `_errors.pyx:163-169`

