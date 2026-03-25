# matplotlib C/C++ Extension Analysis Report

## Executive Summary

matplotlib's C/C++ extensions (`src/`) comprise ~6.6K lines across 13 source files producing 9 extension modules. **8 of 9 modules use pybind11**, which dramatically reduces the surface area for traditional Python/C API bugs — the scanners reported 0 refcount, 0 null, and 0 type slot findings. The code is remarkably well-structured: 0 complexity hotspots, average cyclomatic complexity 3.7, average function length 15.3 lines.

The primary risk is concentrated in **`_macosx.m`** — the only module using raw Python/C API with Objective-C. This single file accounts for nearly all FIX findings. The pybind11-based modules are clean, with findings limited to one inverted error check in `_enums.h` and a `std::string` constructed from a potential NULL in `ft2font.h`.

For **free-threading readiness**, the codebase has the right declarations (`py::mod_gil_not_used()` on all modules) but two significant gaps: the global `FT_Library` is not thread-safe, and GIL is not released during CPU-intensive Agg rendering and Qhull triangulation.

**Total confirmed findings: 8 FIX, 15 CONSIDER, across all agents.**

---

**Project:** matplotlib — Python plotting library C/C++ extensions
**Source:** `~/projects/laruche/repositories/matplotlib/src`
**Stats:** 13 source files + headers, ~6.6K lines, 191 functions, 9 extension modules
**Binding:** pybind11 (8 modules), raw Python/C API (1 module: `_macosx.m`)

---

## Critical Findings (FIX)

### 1. `std::string` constructed from NULL `ft_error_string` — undefined behavior

**File:** `ft2font.h:39,52`

`ft_error_string()` returns NULL for unknown FreeType error codes. `THROW_FT_ERROR` constructs `std::string{ft_error_string(err)}` — constructing `std::string` from `nullptr` is UB (typically crashes in `strlen`). Triggered when FreeType returns an error code not in the known list (e.g., newer FreeType version, corrupted font).

*Found by: null-safety-scanner*

### 2. Inverted `PyErr_Occurred` logic in enum type caster

**File:** `_enums.h:83`

`return !(ival == -1 && !PyErr_Occurred())` returns `true` (success) when `PyLong_AsLong` fails with an active exception. One-character fix: remove the `!` before `PyErr_Occurred()`.

*Found by: error-path-analyzer*

### 3. Unchecked `malloc` + leaked `Py_buffer` in `_copy_agg_buffer`

**File:** `_macosx.m:1221-1225`

`malloc(sizeof(Py_buffer))` is not NULL-checked. If `PyObject_GetBuffer` fails, the buffer is leaked (only `_buffer_release` frees it, which is a CGDataProvider callback registered later).

*Found by: null-safety-scanner, refcount-auditor, git-history-analyzer (3 confirmations)*

### 4. Unchecked `PyOS_double_to_string` — `strlen(NULL)` crash

**File:** `_path.h:1070-1073`

`PyOS_double_to_string()` can return NULL on OOM. `strlen(str)` immediately follows without a NULL check.

*Found by: null-safety-scanner*

### 5. `FigureCanvas_set_cursor` returns NULL without exception

**File:** `_macosx.m:451`

The `default:` case returns NULL without calling `PyErr_Set*`, causing `SystemError`.

*Found by: type-slot-checker, null-safety-scanner, refcount-auditor (3 confirmations)*

### 6. `FigureManager__set_window_mode` returns NULL without exception

**File:** `_macosx.m:653-654`

When `self->window` is NULL (after `destroy()`), returns NULL without setting an exception.

*Found by: null-safety-scanner*

### 7. NULL `char*` streamed to `std::stringstream` in `ft_glyph_warn`

**File:** `ft2font.cpp:457`, `ft2font_wrapper.cpp:410`

`face->family_name` can be NULL. When NULL is inserted into `std::set<FT_String*>` and later streamed via `ss << *it`, it's undefined behavior.

*Found by: null-safety-scanner*

### 8. `NSFileHandle` leak in `wake_on_fd_write`

**File:** `_macosx.m:246-257`

`[[NSFileHandle alloc] initWithFileDescriptor: fd]` is never released. The notification center retains it, but the `alloc` retain count is never balanced.

*Found by: git-history-analyzer*

---

## Important Findings (CONSIDER)

### Performance — GIL not released during expensive operations

| Operation | File | Impact |
|-----------|------|--------|
| Agg rendering (draw_path, draw_markers, draw_path_collection, etc.) | `_backend_agg_wrapper.cpp:41-215` | HIGH — blocks all threads during software rasterization |
| Qhull Delaunay triangulation | `_qhull_wrapper.cpp:138-253` | HIGH — blocks during large point set triangulation |
| Path computations (points_in_path, cleanup_path, etc.) | `_path_wrapper.cpp:33-304` | MEDIUM — depends on dataset size |
| FreeType text/glyph operations | `ft2font_wrapper.cpp:714-957` | POLICY — interleaves Python callbacks |

Note: `_image_wrapper.cpp` already correctly releases the GIL during resampling, demonstrating the pattern.

*Found by: gil-discipline-checker*

### Free-threading concerns

| Concern | File | Description |
|---------|------|-------------|
| Global `FT_Library _ft2Library` | `ft2font.cpp:44` | FreeType is not thread-safe for shared library instances |
| FreeType stream read callback | `ft2font_wrapper.cpp:367-388` | Calls Python I/O without GIL guard on free-threaded builds |
| `ft_glyph_warn` calls Python APIs | `ft2font_wrapper.cpp:405-418` | Module import/attr access without GIL on free-threaded builds |
| Global `p11x::enums` map | `_enums.h:37` | `std::unordered_map` with `py::object` values, no synchronization |

*Found by: gil-discipline-checker*

### macOS backend lifecycle

| Concern | File | Description |
|---------|------|-------------|
| `NSTrackingArea` leak in `FigureCanvas_init` | `_macosx.m:378-382` | `alloc` without matching `release` |
| `Window.close` double-decref risk | `_macosx.m:1177-1188` | `Py_DECREF(manager)` if `dealloc` runs without prior `destroy` |
| `PyFT2Font_init` leaks on exception | `ft2font_wrapper.cpp:457-505` | Raw `new` without `unique_ptr` |
| Timer NSTimer captures raw `self` pointer | `_macosx.m:1807-1816` | Block captures Python object without `Py_INCREF` |
| View stores raw `canvas` pointer | `_macosx.m:139` | No `Py_INCREF`, potential dangling pointer |
| Exception clobbering in ft2font catch-all | `ft2font_wrapper.cpp:492-494` | `catch(std::exception&)` replaces meaningful errors with `TypeError` |
| Unchecked `PyUnicode_EncodeFSDefault` | `_tkagg.cpp:333-335` | NULL wrapped in `py::bytes` |

*Found by: type-slot-checker, refcount-auditor, null-safety-scanner, error-path-analyzer*

### Deprecated/dead code

| Item | File | Description |
|------|------|-------------|
| `PyErr_Fetch`/`PyErr_Restore` | `ft2font_wrapper.cpp:394,402` | Deprecated since 3.12 |
| `PY_SSIZE_T_CLEAN` defines (×3) | `mplutils.h`, `py_adaptors.h`, `_macosx.m` | No-op since 3.10 |
| Solaris `_XPG4`/`_XPG3` undefs | `mplutils.h:22-28` | Likely dead |
| macOS SDK < 10.14 compat defines | `_macosx.m:11-16` | Likely dead |

*Found by: version-compat-scanner*

---

## Architecture & Migration Assessment

| Aspect | Status |
|--------|--------|
| **Binding framework** | pybind11 (8 modules), raw Python/C API (1: `_macosx.m`) |
| **Init style** | Single-phase (all 9 — pybind11 limitation + `_macosx.m`) |
| **Static types** | 4 (all in `_macosx.m`) |
| **Stable ABI** | Not feasible (pybind11 incompatible; nanobind is realistic long-term path) |
| **Free-threading** | All declare `mod_gil_not_used`; gaps in FreeType thread safety and GIL release |
| **Complexity** | Excellent (0 hotspots, avg CC 3.7) |
| **macOS backend** | `_macosx.m` is the primary risk — only raw C API file, ObjC memory management |

---

## Summary Table

| # | Finding | Classification | Confidence | Source |
|---|---------|---------------|------------|--------|
| 1 | `std::string{nullptr}` in `THROW_FT_ERROR` | **FIX** | HIGH | null |
| 2 | Inverted `PyErr_Occurred` in enum caster | **FIX** | HIGH | error-path |
| 3 | Unchecked `malloc` + leaked `Py_buffer` | **FIX** | HIGH | null, refcount, git |
| 4 | `strlen(NULL)` from `PyOS_double_to_string` | **FIX** | HIGH | null |
| 5 | `set_cursor` NULL without exception | **FIX** | HIGH | type-slot, null, refcount |
| 6 | `_set_window_mode` NULL without exception | **FIX** | HIGH | null |
| 7 | NULL `char*` to `stringstream` in `ft_glyph_warn` | **FIX** | MEDIUM | null |
| 8 | `NSFileHandle` leak in `wake_on_fd_write` | **FIX** | MEDIUM | git |
| 9 | GIL held during Agg rendering | CONSIDER | HIGH | gil |
| 10 | GIL held during Qhull triangulation | CONSIDER | HIGH | gil |
| 11 | Global `FT_Library` not thread-safe | CONSIDER | MEDIUM | gil |
| 12 | FreeType callbacks lack GIL guard for free-threading | CONSIDER | MEDIUM | gil |
| 13-15 | ObjC lifecycle concerns (Window, Timer, View) | CONSIDER | MEDIUM | type-slot, refcount |
| 16 | `PyFT2Font_init` memory leak on exception | CONSIDER | MEDIUM | type-slot, error-path |
| 17-18 | `NSTrackingArea` leak, `p11x::enums` thread safety | CONSIDER | MEDIUM | git, gil |
| 19-23 | Deprecated APIs, dead compat code | CONSIDER | HIGH | version-compat |

---

## Priority Recommendations

1. **Findings 1-2 (UB/logic bugs)** are the highest priority — `std::string{nullptr}` crashes on unknown FreeType errors, and the inverted enum error check violates pybind11's exception contract. Both are one-line fixes.

2. **Findings 3-6 (NULL/exception bugs in `_macosx.m`)** — `_macosx.m` is the only file using raw Python/C API and accounts for most FIX findings. Adding NULL checks and `PyErr_Set*` calls is straightforward.

3. **Finding 9 (Agg GIL release)** is the biggest performance opportunity — releasing the GIL during `draw_path_collection` and other Agg rendering calls would allow other threads to run during matplotlib's most CPU-intensive operation. The pattern already exists in `_image_wrapper.cpp`.

4. **Finding 11 (FreeType thread safety)** is the most important free-threading concern — the global `FT_Library` needs either a mutex or thread-local instances before free-threaded Python can safely use ft2font concurrently.

5. **Dead compat code cleanup** (findings 19-23) is low-risk, high-clarity — removing `PY_SSIZE_T_CLEAN`, Solaris undefs, and old macOS SDK guards.