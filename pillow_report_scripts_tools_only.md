# Pillow C Extension Analysis Report

## Extension: Pillow (PIL)
**Scope:** `src/` — 87 C files, 26 headers, ~14,000 lines
**Modules:** 7 init functions (`_imaging`, `_imagingcms`, `_imagingft`, `_imagingmath`, `_imagingmorph`, `_imagingtk`, `_avif`)
**Init style:** Single-phase (all modules use `PyModule_Create`)
**Limited API:** No
**Min Python:** 3.10 (detected from project config)

## Script Results Summary

| Script | Functions | Findings | Key Types |
|--------|-----------|----------|-----------|
| scan_refcounts | 1,041 | 19 | 7 borrowed_ref_across_call, 10 leak_on_error, 2 leak |
| scan_error_paths | 1,041 | 409 | 338 return_without_exception, 53 missing_null_check, 18 clobbering |
| scan_null_checks | 1,041 | 22 | 16 unchecked_alloc, 5 deref_before_check, 1 deref_macro |
| scan_gil_usage | 1,041 | 2 | 1 callback_without_gil, 1 blocking_with_gil |
| scan_module_state | 1,041 | 88 | 66 static_mutable, 15 static_type, 7 AddObject misuse |
| scan_type_slots | 1,041 | 27 | 12 dealloc_missing_xdecref, 11 dealloc_wrong_free, 4 missing_tp_free |
| scan_version_compat | 1,041 | 23 | 16 dead_version_guard, 7 deprecated_api |
| measure_c_complexity | 1,041 | 3 hotspots | Top: font_render (7.5), FliDecode (5.5), polygon_generic (5.2) |
| analyze_history | 50 commits | 9 fixes | Top churn: encode.c (5 commits), decode.c (3) |

## Key Findings by Priority

### FIX — Crash/Leak Risk

1. **[refcount] 7 borrowed-ref-across-call** — High confidence. Borrowed references from `PyTuple_GET_ITEM`, `PyModule_GetDict` etc. used after intervening Python calls. Most critical in `_avif.c:setup_module` and `_imaging.c:_getxy`.

2. **[type-slots] 4 dealloc_missing_tp_free** — `_encoder_dealloc` and `_decoder_dealloc` in `_avif.c` don't call `tp_free` — memory leak on every encoder/decoder destruction.

3. **[type-slots] 12 dealloc_missing_xdecref** — PyObject* struct members not cleaned up in dealloc across `_avif.c` encoders/decoders. Reference leaks per object destruction.

4. **[null-checks] 16 unchecked_alloc** — `malloc`/`PyMem_Malloc` results used without NULL check, including in `_imaging.c` Arrow capsule exports and `_getprojection`.

5. **[null-checks] 5 deref_before_check** — Pointers dereferenced before NULL check.

6. **[error-paths] 53 missing_null_check** — New-ref API results used without checking for NULL.

7. **[refcount] 10 potential_leak_on_error** — References acquired but not released on error paths.

### CONSIDER — Improvements

8. **[type-slots] 11 dealloc_wrong_free** — Using `PyObject_Del`/`PyObject_Free` instead of `Py_TYPE(self)->tp_free` — breaks inheritance.

9. **[version-compat] 16 dead_version_guard** — `#if PY_VERSION_HEX < 0x030A0000` blocks are dead code since min Python is 3.10.

10. **[version-compat] 7 deprecated_api** — `PyModule_AddObject` used in `_imaging.c` setup_module (should use `PyModule_AddObjectRef`).

11. **[module-state] 15 static_type_object** — Static `PyTypeObject` definitions should be heap types for multi-phase init.

12. **[module-state] 66 static_mutable_state** — Significant global mutable state, primarily in `tkImaging.c` and format tables.

13. **[gil] 1 blocking_with_gil** — `_encode_to_file` in `encode.c` calls `fwrite` with GIL held.

### Complexity Hotspots

14. **font_render** (`_imagingft.c:822`) — Score 7.5, 357 lines, cyclomatic 63, nesting 9. The most complex function by far.

15. **ImagingFliDecode** (`libImaging/FliDecode.c:31`) — Score 5.5, 198 lines, cyclomatic 48.

16. **polygon_generic** (`libImaging/Draw.c:476`) — Score 5.2, 129 lines, cyclomatic 40, nesting 8.

### Git History Context

- 50 recent commits from 8 authors over 77 days
- 9 fix commits (18% fix rate) — moderate
- `encode.c` and `decode.c` are the highest-churn C files
- Recent fix: "Fix OOB Write with invalid tile extents" — suggests boundary checking patterns worth searching for elsewhere

## Strengths

- Very low GIL discipline issues (only 2 findings across 1,041 functions)
- Average cyclomatic complexity of 5.6 is reasonable for a project this size
- Only 3 complexity hotspots out of 1,041 functions (0.3%)
- Active maintenance with 8 contributors

## Recommended Action Plan

### Immediate
1. Fix `_avif.c` dealloc functions — missing `tp_free` and missing `Py_XDECREF` for struct members
2. Add NULL checks for unchecked allocations in `_imaging.c` Arrow exports
3. Fix borrowed-ref-across-call in `_avif.c:setup_module` and `_imaging.c:_getxy`

### Short-term
4. Replace `PyModule_AddObject` with `PyModule_AddObjectRef` in all setup functions
5. Remove 16 dead version guard blocks (targeting Python < 3.10)
6. Release GIL around `fwrite` in `encode.c:_encode_to_file`

### Longer-term
7. Consider multi-phase init migration (15 static types to convert)
8. Refactor `font_render` (357 lines, score 7.5) — the only critical complexity hotspot