# h5py C Extension Analysis Report

## Executive Summary

h5py is a large Cython extension (~29K `.pyx` source, ~613K generated C) wrapping the HDF5 C library for Python access to HDF5 files. The analysis found that **Cython handles Python-level reference counting correctly** (1290 scanner findings were almost entirely false positives), but h5py has **8 confirmed HDF5 resource lifecycle bugs** — handles to HDF5 types, dataspaces, and memory buffers that leak on error paths or are never closed at all.

The GIL discipline is **exemplary** — h5py uses a well-designed two-layer pattern (application-level `_phil` lock + GIL release during HDF5 I/O) that was verified as correct across all 18 `nogil` blocks and all VFD callbacks.

Additionally, the git history analysis found **3 unchecked `malloc` calls** that segfault on OOM, a **`readinto` return value not checked** in the VFD driver (data corruption risk), and **resource leak patterns in `_selector.pyx`** that mirror recently-fixed bugs.

**Total: 8 FIX, 7 CONSIDER.**

---

## Extension Profile

| Attribute | Value |
|-----------|-------|
| **Cython source** | 37 `.pyx` files (~29K lines) |
| **Generated C** | 25 files (~613K lines, Cython 3.2.4) |
| **Vendored** | lzf compression filter (3 files) |
| **Functions** | 5258 (generated), 55 complexity hotspots |
| **Architecture** | `ObjectID` base class wraps HDF5 handles with Python refcounting; `_phil` FastRLock for thread safety; GIL released during HDF5 I/O |

## Key Metrics

| Dimension | Status | FIX | CONSIDER | Top Finding |
|-----------|--------|-----|----------|-------------|
| HDF5 Handle Lifecycle | 🔴 | 5 | 2 | Type handles leaked in `check_compound_complex` (4 per call) |
| NULL Safety | 🟡 | 3 | 1 | Unchecked `malloc` in VFD driver, `h5i.get_name`, `h5r.get_name` |
| GIL Discipline | 🟢 | 0 | 1 | Exemplary — verified correct across all nogil blocks |
| Python Refcounting | 🟢 | 0 | 0 | Cython handles correctly; 1290 scanner findings all false positives |
| Type Slots | 🟢 | 0 | 0 | All sentinels valid; heap type lifecycle correct |
| Error Paths | 🟡 | 0 | 3 | `readinto` unchecked, `_dealloc` doesn't zero id, Reader raw hid_t |

---

## Findings by Priority

### Must Fix (FIX)

**HDF5 Handle Leaks:**

**1. `check_compound_complex` leaks 4 HDF5 type handles per call**
`H5Tget_member_type()` results are used inline in a boolean expression and discarded without `H5Tclose`. Each call leaks 4 type handles. Affects HDF5 ≥ 2.0 builds.
- `_conv.templ.pyx:915-927`
- *Source: refcount-auditor*

**2. `h5_vlen_string` never closed in `dset_rw_vlen_strings`**
`H5Tcopy(H5T_C_S1)` result is missing from the finally block. Leaks one type handle per vlen string read/write — proportional to I/O volume.
- `_proxy.pyx:191-231`
- *Source: refcount-auditor*

**3. `_npystrings_pack` missing try/finally**
If `H5Diterate` raises, both the string allocator and HDF5 type handle leak. The matching `_npystrings_unpack` correctly uses try/finally — this is an oversight.
- `_npystrings.pyx:100-121`
- *Sources: refcount-auditor, git-history-analyzer*

**4. Space handle leak in `write_direct_chunk` on validation error**
`H5Dget_space` called before the try block; if `len(offsets) != rank` raises, the space handle is never closed.
- `h5d.pyx:516-531`
- *Source: refcount-auditor*

**5. Space handle + buffer leak in `get_chunk_info` on error**
No try/finally at all — any HDF5 error leaks both the space handle and the malloc'd offset buffer.
- `h5d.pyx:621-653`
- *Source: refcount-auditor*

**Unchecked malloc (segfault on OOM):**

**6. `H5FD_fileobj_open` — unchecked malloc + immediate deref**
`stdlib_malloc(sizeof(H5FD_fileobj_t))` result used on the next line without NULL check. Segfault on OOM during any file-object open.
- `h5fd.pyx:132-135`
- *Source: null-safety-scanner*

**7. `h5i.get_name` — unchecked malloc**
`malloc(sizeof(char)*(namelen+1))` passed to `H5Iget_name` without NULL check. Segfault when getting any HDF5 object name under OOM.
- `h5i.pyx:101`
- *Source: null-safety-scanner*

**8. `h5r.get_name` — unchecked malloc**
Same pattern as `h5i.get_name`. Should use `emalloc` (which checks for NULL) instead of raw `malloc`.
- `h5r.pyx:141`
- *Source: null-safety-scanner*

### Should Consider (CONSIDER)

| # | Finding | File | Source |
|---|---------|------|--------|
| 1 | `build_fancy_hyperslab` leaks `space2` on exception — recursive, can leak multiple handles | `_selector.pyx:247-262` | git-history |
| 2 | `readinto` return value unchecked in VFD driver — partial reads leave buffer uninitialized (data corruption) | `h5fd.templ.pyx:156-168` | git-history |
| 3 | `temptype` leak in `make_reduced_type` loop iteration | `_proxy.pyx:271-288` | git-history |
| 4 | `ObjectID._dealloc` doesn't zero `self.id` after `H5Idec_ref` (defensive improvement) | `_objects.pyx:209-218` | type-slot |
| 5 | `Reader` holds raw `hid_t` without HDF5 refcount increment (fragile pattern) | `_selector.pyx:326-327` | type-slot |
| 6 | Unbalanced `Py_INCREF(elem_dtype)` in vlen conversion — dtype ref never released | `_conv.pyx:760` | refcount |
| 7 | Static `_error_handler` data race under free-threading (PEP 703) | `_errors.pyx:163-169` | null+gil |

---

## Strengths

1. **GIL discipline is exemplary** — two-layer locking (`_phil` + GIL release), verified correct across all 18 `nogil` blocks and all VFD callbacks. No Python objects accessed without GIL.
2. **Cython generates correct Python refcounting** — 1290 scanner findings were all false positives. The `__Pyx_GOTREF`/`__Pyx_GIVEREF` tracking is reliable.
3. **Type slot correctness** — ObjectID base class properly manages HDF5 handles through `__dealloc__` under the global lock. Heap type lifecycle is correct.
4. **Thread safety architecture is sound** — `FastRLock` with careful GIL release, `noexcept nogil` for pure-C callbacks, `with gil:` for Python-touching callbacks.
5. **Active maintenance** — 31 fix commits in 113 days, recent TSAN fixes for race conditions, ongoing complex-number feature work.

---

## Recommended Action Plan

### Immediate
1. Add `H5Tclose` for member types in `check_compound_complex` — store in local vars, close in finally
2. Add `H5Tclose(h5_vlen_string)` to finally block in `dset_rw_vlen_strings`
3. Wrap `_npystrings_pack` body in try/finally (matching `_npystrings_unpack`)
4. Add NULL check after `stdlib_malloc` in `H5FD_fileobj_open`
5. Replace `malloc` with `emalloc` in `h5i.get_name` and `h5r.get_name`

### Short-term
6. Move `H5Dget_space` inside try blocks in `write_direct_chunk` and `get_chunk_info`
7. Add try/finally for `space2` in `build_fancy_hyperslab`
8. Check `readinto` return value in VFD driver (match the `read` path's check)

### Longer-term
9. Audit all raw `hid_t` usage outside try/finally for exception safety
10. Consider `_dealloc` zeroing `self.id` for defensive shutdown behavior
11. Address free-threading concerns when targeting PEP 703