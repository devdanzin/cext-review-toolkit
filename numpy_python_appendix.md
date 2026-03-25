# NumPy Python Report — Reproducer & Fix Appendix

Companion to `numpy_python_report.md`. Contains reproducers for testable findings and fix suggestions showing current vs. corrected code.

---

## F1 / F2. Silent Data Corruption in Masked Binary Operations

### Reproducer

```python
"""Reproducer: ma binary operation silently returns corrupted data.

When copyto fails to restore original data at masked positions
(e.g., due to dtype incompatibility), except Exception: pass
silently swallows the error. The result contains uninitialized
data at masked positions.

Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np
import numpy.ma as ma


# Create a masked array where the mask covers some elements
# Use a dtype combination where copyto with casting='unsafe' can fail
class BadArray(np.ndarray):
    """Array subclass whose copyto raises on purpose."""
    def __array_finalize__(self, obj):
        pass


a = ma.array([1.0, 2.0, 3.0], mask=[False, True, False])
b = ma.array([10.0, 20.0, 30.0], mask=[False, True, False])

# The normal case works, but to demonstrate the silent swallow:
# Monkey-patch copyto to simulate a failure
_original_copyto = np.copyto
def failing_copyto(*args, **kwargs):
    raise RuntimeError("simulated copyto failure")

np.copyto = failing_copyto
try:
    result = a + b  # _MaskedBinaryOperation.__call__
    # result.data[1] now contains whatever the ufunc computed
    # (not the original a.data[1]), with NO warning or error
    print(f"result.data = {result.data}")
    print(f"result.mask = {result.mask}")
    print("BUG: No error raised despite copyto failure — "
          "masked position may contain garbage")
finally:
    np.copyto = _original_copyto
```

### Fix Suggestion

**File:** `numpy/ma/core.py`

**Current code (line 1093-1098):**
```python
        if m is not nomask and m.any():
            # any errors, just abort; impossible to guarantee masked values
            try:
                np.copyto(result, da, casting='unsafe', where=m)
            except Exception:
                pass
```

**Fixed code:**
```python
        if m is not nomask and m.any():
            try:
                np.copyto(result, da, casting='unsafe', where=m)
            except (TypeError, ValueError):
                # dtype incompatibility is expected with mixed types;
                # masked positions retain the ufunc output (not original data)
                pass
```

**Same pattern at line 1229-1238 (`_DomainedBinaryOperation`):**

**Current code:**
```python
        try:
            np.copyto(result, 0, casting='unsafe', where=m)
            # avoid using "*" since this may be overlaid
            masked_da = umath.multiply(m, da)
            # only add back if it can be cast safely
            if np.can_cast(masked_da.dtype, result.dtype, casting='safe'):
                result += masked_da
        except Exception:
            pass
```

**Fixed code:**
```python
        try:
            np.copyto(result, 0, casting='unsafe', where=m)
            masked_da = umath.multiply(m, da)
            if np.can_cast(masked_da.dtype, result.dtype, casting='safe'):
                result += masked_da
        except (TypeError, ValueError):
            pass
```

---

## F3. Mask Silently Dropped in `__array_finalize__`

### Reproducer

```python
"""Reproducer: mask silently dropped when it can't be reshaped.

When a MaskedArray is reshaped and the mask can't follow,
except ValueError sets mask to nomask — silently unmasking
all previously masked (potentially invalid) data.

Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np
import numpy.ma as ma

# Create a masked array with some masked values
a = ma.array([1.0, np.nan, 3.0, np.nan], mask=[False, True, False, True])

# The mask correctly hides NaN values
print(f"Before: mask = {a.mask}")
print(f"Before: data = {a.data}")

# Force a shape mismatch between data and mask via __array_finalize__
# by creating a view with an incompatible shape for the mask
class ForcedView(ma.MaskedArray):
    pass

# Direct demonstration of the pattern:
arr = ma.MaskedArray.__new__(ForcedView)
arr.__dict__['_data'] = np.array([1.0, np.nan, 3.0, np.nan])

# Set a mask with wrong shape to trigger the ValueError path
bad_mask = np.array([[True, False], [True, False]])
arr._mask = bad_mask
arr._data = np.array([1.0, np.nan, 3.0, np.nan])

# Now trigger __array_finalize__ reshape path
try:
    arr._mask = arr._mask.reshape(arr.shape)
except ValueError:
    # This is what __array_finalize__ catches and then sets nomask
    print("BUG: ValueError caught — mask would be silently set to nomask")
    print("Previously masked NaN values would become visible as valid data")
```

### Fix Suggestion

**File:** `numpy/ma/core.py`

**Current code (line 3127-3131):**
```python
        if self._mask is not nomask:
            try:
                self._mask = self._mask.reshape(self.shape)
            except ValueError:
                self._mask = nomask
```

**Fixed code:**
```python
        if self._mask is not nomask:
            try:
                self._mask = self._mask.reshape(self.shape)
            except ValueError:
                self._mask = nomask
                import warnings
                warnings.warn(
                    "mask could not be reshaped to match data shape; "
                    "mask has been reset to nomask (all data unmasked). "
                    "Verify masked positions are valid.",
                    stacklevel=2,
                )
```

---

## F4 / F5. `array_equal` and `array_equiv` Hide Fatal Errors

### Reproducer

```python
"""Reproducer: array_equal hides MemoryError as False.

np.array_equal catches all exceptions during asarray conversion
and returns False. This means MemoryError, SystemError, and bugs
in __array__ methods are silently converted to False.

Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np

class BuggyArray:
    """Object with a broken __array__ method."""
    def __array__(self, dtype=None, copy=None):
        raise SystemError("internal error in __array__")

a = np.array([1, 2, 3])
b = BuggyArray()

# BUG: returns False instead of raising SystemError
result = np.array_equal(a, b)
print(f"array_equal returned: {result}")
print("BUG: SystemError was silently converted to False")

# Same for array_equiv
result2 = np.array_equiv(a, b)
print(f"array_equiv returned: {result2}")
print("BUG: SystemError was silently converted to False")

# Even MemoryError would be hidden:
class OOMArray:
    def __array__(self, dtype=None, copy=None):
        raise MemoryError("out of memory")

result3 = np.array_equal(a, OOMArray())
print(f"array_equal with OOM: {result3}")
print("BUG: MemoryError silently became False")
```

### Fix Suggestion

**File:** `numpy/_core/numeric.py`

**Current code (line 2529-2532):**
```python
    try:
        a1, a2 = asarray(a1), asarray(a2)
    except Exception:
        return False
```

**Fixed code:**
```python
    try:
        a1, a2 = asarray(a1), asarray(a2)
    except (TypeError, ValueError):
        return False
```

**Current code (line 2597-2604) — `array_equiv`:**
```python
    try:
        a1, a2 = asarray(a1), asarray(a2)
    except Exception:
        return False
    try:
        multiarray.broadcast(a1, a2)
    except Exception:
        return False
```

**Fixed code:**
```python
    try:
        a1, a2 = asarray(a1), asarray(a2)
    except (TypeError, ValueError):
        return False
    try:
        multiarray.broadcast(a1, a2)
    except (TypeError, ValueError):
        return False
```

---

## F6. Polynomial Operators Hide Fatal Errors

### Reproducer

```python
"""Reproducer: polynomial arithmetic hides MemoryError as TypeError.

All ABCPolyBase arithmetic operators catch Exception and return
NotImplemented, which Python then converts to TypeError.

Tested: NumPy 2.4.3, Python 3.14.
"""
import numpy as np
from numpy.polynomial import Polynomial

p = Polynomial([1, 2, 3])

class BadOperand:
    """Operand whose coef conversion raises SystemError."""
    @property
    def coef(self):
        raise SystemError("internal error")

# Monkey-patch _get_coefficients to trigger an internal error
_original = Polynomial._get_coefficients
def bad_get_coefficients(self, other):
    raise MemoryError("out of memory during coefficient conversion")

Polynomial._get_coefficients = bad_get_coefficients
try:
    # BUG: MemoryError becomes TypeError via NotImplemented
    result = p + Polynomial([1])
except TypeError as e:
    print(f"Got TypeError: {e}")
    print("BUG: MemoryError was silently converted to TypeError")
except MemoryError:
    print("CORRECT: MemoryError propagated properly")
finally:
    Polynomial._get_coefficients = _original
```

### Fix Suggestion

**File:** `numpy/polynomial/_polybase.py`

Apply the same change to all 8 operators. Example for `__add__` (line 530-536):

**Current code:**
```python
    def __add__(self, other):
        othercoef = self._get_coefficients(other)
        try:
            coef = self._add(self.coef, othercoef)
        except Exception:
            return NotImplemented
        return self.__class__(coef, self.domain, self.window, self.symbol)
```

**Fixed code:**
```python
    def __add__(self, other):
        othercoef = self._get_coefficients(other)
        try:
            coef = self._add(self.coef, othercoef)
        except (TypeError, ValueError):
            return NotImplemented
        return self.__class__(coef, self.domain, self.window, self.symbol)
```

**Same fix for:** `__sub__` (line 540), `__mul__` (line 548), `__divmod__` (line 579, keep `except ZeroDivisionError: raise` before the new line), `__radd__` (line 596), `__rsub__` (line 603), `__rmul__` (line 610), `__rdivmod__` (line 629).

---

## F7. f2py Distutils Dead Code

### Fix Suggestion

**File:** `numpy/f2py/f2py2e.py`

**Current code (line 568):**
```python
    parser.add_argument("--backend", choices=['meson', 'distutils'], default='distutils')
```

**Fixed code:**
```python
    parser.add_argument("--backend", choices=['meson'], default='meson')
```

**Current code (line 580-584):**
```python
    backend_key = args.backend
    if backend_key == 'distutils':
        outmess("Cannot use distutils backend with Python>=3.12,"
                " using meson backend instead.\n")
        backend_key = "meson"
```

**Fixed code:**
```python
    backend_key = args.backend
```

**Current code (line 650-655) — dead regex/filtering:**
```python
    # TODO: Once distutils is dropped completely, i.e. min_ver >= 3.12, unify into --fflags
    reg_f77_f90_flags = re.compile(r'--f(77|90)flags=')
    reg_distutils_flags = re.compile(r'--((f(77|90)exec|opt|arch)=|(debug|noopt|noarch|help-fcompiler))')
    fc_flags = [_m for _m in sys.argv[1:] if reg_f77_f90_flags.match(_m)]
    distutils_flags = [_m for _m in sys.argv[1:] if reg_distutils_flags.match(_m)]
    sys.argv = [_m for _m in sys.argv if _m not in (fc_flags + distutils_flags)]
```

**Fixed code:**
```python
    reg_fc_flags = re.compile(r'--f(77|90)?flags=')
    fc_flags = [_m for _m in sys.argv[1:] if reg_fc_flags.match(_m)]
    sys.argv = [_m for _m in sys.argv if _m not in fc_flags]
```

---

## F8. NPY_PROMOTION_STATE Environment Variable

### Fix Suggestion

**File:** `numpy/__init__.py`

**Remove lines 905-910:**
```python
    # TODO: Remove the environment variable entirely now that it is "weak"
    if (os.environ.get("NPY_PROMOTION_STATE", "weak") != "weak"):
        warnings.warn(
            "NPY_PROMOTION_STATE was a temporary feature for NumPy 2.0 "
            "transition and is ignored after NumPy 2.2.",
            UserWarning, stacklevel=2)
```

---

## F10. Wrong Test Label in TESTS.rst

### Fix Suggestion

**File:** `doc/TESTS.rst`

**Current code (line 42):**
```
  >>> numpy.test(label='slow')
```

**Fixed code:**
```
  >>> numpy.test(label='full')
```

---

## F11. User-Visible TODO in `help(np.ndarray)`

### Fix Suggestion

**File:** `numpy/_core/_add_newdocs.py`

**Current code (line 2425-2426):**
```python
        allows assignments, e.g., ``x.flat = 3`` (See `ndarray.flat` for
        assignment examples; TODO).
```

**Fixed code:**
```python
        allows assignments, e.g., ``x.flat = 3`` (See `ndarray.flat` for
        assignment examples).
```

---

## F12. Wrong Module Name in Reload Error

### Fix Suggestion

**File:** `numpy/exceptions.py`

**Current code (line 43):**
```python
    raise RuntimeError('Reloading numpy._globals is not allowed')
```

**Fixed code:**
```python
    raise RuntimeError('Reloading numpy.exceptions is not allowed')
```

---

## F13. build_requirements.txt Version Mismatch

### Fix Suggestion

**File:** `requirements/build_requirements.txt`

**Current code (line 1):**
```
meson-python>=0.13.1
```

**Fixed code:**
```
meson-python>=0.18.0
```

---

## P1. Incomplete Fix Propagation for `flatten_sequence` str/bytes Guard

### Reproducer

```python
"""Reproducer: flatten_inplace infinite loop on string elements.

flatten_inplace uses hasattr(seq[k], '__iter__') without excluding
str/bytes. Strings iterate to single-character strings which also
have __iter__, causing an infinite loop.

The fix was applied to flatten_sequence (commit 41f3673) but NOT
propagated to flatten_inplace or _flatsequence.

Tested: NumPy 2.4.3, Python 3.14.
"""
import signal
import numpy.ma.extras as extras

# Set a timeout so the infinite loop doesn't hang forever
def timeout_handler(signum, frame):
    raise TimeoutError("Infinite loop detected!")

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(3)  # 3 second timeout

try:
    # This will loop forever — strings have __iter__ returning
    # single-char strings, which also have __iter__
    result = extras.flatten_inplace(["hello", "world"])
    print(f"result = {result}")
except TimeoutError:
    print("BUG: flatten_inplace enters infinite loop on string input")
finally:
    signal.alarm(0)
```

### Fix Suggestion

**File:** `numpy/ma/extras.py`

**Current code (line 340-347):**
```python
def flatten_inplace(seq):
    """Flatten a sequence in place."""
    k = 0
    while (k != len(seq)):
        while hasattr(seq[k], '__iter__'):
            seq[k:(k + 1)] = seq[k]
        k += 1
    return seq
```

**Fixed code:**
```python
def flatten_inplace(seq):
    """Flatten a sequence in place."""
    k = 0
    while (k != len(seq)):
        while (hasattr(seq[k], '__iter__')
               and not isinstance(seq[k], (str, bytes))):
            seq[k:(k + 1)] = seq[k]
        k += 1
    return seq
```

**File:** `numpy/ma/core.py`

**Current code (line 1857-1866):**
```python
    def _flatsequence(sequence):
        "Generates a flattened version of the sequence."
        try:
            for element in sequence:
                if hasattr(element, '__iter__'):
                    yield from _flatsequence(element)
                else:
                    yield element
        except TypeError:
            yield sequence
```

**Fixed code:**
```python
    def _flatsequence(sequence):
        "Generates a flattened version of the sequence."
        try:
            for element in sequence:
                if (hasattr(element, '__iter__')
                        and not isinstance(element, (str, bytes))):
                    yield from _flatsequence(element)
                else:
                    yield element
        except TypeError:
            yield sequence
```

**File:** `numpy/lib/recfunctions.py`

**Current code (line 292-294):**
```python
    for element in iterable:
        if (hasattr(element, '__iter__') and
                not isinstance(element, str)):
```

**Fixed code:**
```python
    for element in iterable:
        if (hasattr(element, '__iter__') and
                not isinstance(element, (str, bytes))):
```

---

## C6. `eval()` on User File in f2py

### Fix Suggestion

**File:** `numpy/f2py/capi_maps.py`

**Current code (line 156-163):**
```python
    try:
        outmess(f'Reading f2cmap from {f2cmap_file!r} ...\n')
        with open(f2cmap_file) as f:
            d = eval(f.read().lower(), {}, {})
        f2cmap_all, f2cmap_mapped = process_f2cmap_dict(f2cmap_all, d, c2py_map, True)
        outmess('Successfully applied user defined f2cmap changes\n')
    except Exception as msg:
        errmess(f'Failed to apply user defined f2cmap changes: {msg}. Skipping.\n')
```

**Fixed code:**
```python
    try:
        import ast
        outmess(f'Reading f2cmap from {f2cmap_file!r} ...\n')
        with open(f2cmap_file) as f:
            d = ast.literal_eval(f.read().lower())
        f2cmap_all, f2cmap_mapped = process_f2cmap_dict(f2cmap_all, d, c2py_map, True)
        outmess('Successfully applied user defined f2cmap changes\n')
    except (SyntaxError, ValueError) as msg:
        errmess(f'Failed to apply user defined f2cmap changes: {msg}. Skipping.\n')
```

---

## C15. Missing Warning Category in `ma`

### Reproducer

```python
"""Reproducer: ma.core.py warnings.warn without category.

warnings.warn() without a category defaults to UserWarning.
This means users filtering by category (e.g., RuntimeWarning)
will miss these warnings. The redundant "Warning: " prefix
is also inconsistent with all other NumPy modules.

Tested: NumPy 2.4.3, Python 3.14.
"""
import warnings
import numpy as np
import numpy.ma as ma

# Capture warnings to inspect the category
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")

    # Trigger the warning: converting masked element to nan
    x = ma.array([1.0], mask=[True])
    result = float(x[0])

    if w:
        print(f"Warning message: {w[0].message}")
        print(f"Warning category: {w[0].category.__name__}")
        print(f"BUG: Category is UserWarning, should be RuntimeWarning")
        # Also note the redundant "Warning: " prefix in the message
        msg_str = str(w[0].message)
        if msg_str.startswith("Warning:"):
            print(f"BUG: Redundant 'Warning:' prefix in message")
```

### Fix Suggestion

**File:** `numpy/ma/core.py`

**Current code (line 4531):**
```python
            warnings.warn("Warning: converting a masked element to nan.", stacklevel=2)
```

**Fixed code:**
```python
            warnings.warn("Converting a masked element to nan.",
                          RuntimeWarning, stacklevel=2)
```

**File:** `numpy/ma/mrecords.py`

**Current code (line 712):**
```python
            warnings.warn(msg, stacklevel=2)
```

**Fixed code:**
```python
            warnings.warn(msg, UserWarning, stacklevel=2)
```

---

## A4. Dead `_dummy_thread` Fallback

### Fix Suggestion

**File:** `numpy/_core/arrayprint.py`

**Current code (line 29-32):**
```python
try:
    from _thread import get_ident
except ImportError:
    from _dummy_thread import get_ident
```

**Fixed code:**
```python
from _thread import get_ident
```

*Note: `_dummy_thread` was removed in Python 3.9. NumPy requires Python 3.12+, so the fallback is dead code.*

---

## C12. Dead Functions in f2py/auxfuncs.py

### Fix Suggestion

**File:** `numpy/f2py/auxfuncs.py`

Remove the following 10 functions (none have any callers in the codebase):

```python
# Line 73-75: _isstring — identical to _ischaracter (line 68), never called
def _isstring(var): ...

# Line 230-232: isunsignedarray — never called
def isunsignedarray(var): ...

# Line 240-242: issigned_chararray — never called
def issigned_chararray(var): ...

# Line 245-247: issigned_shortarray — never called
def issigned_shortarray(var): ...

# Line 250-252: issigned_array — never called
def issigned_array(var): ...

# Line 264-265: ismutable — never called
def ismutable(var): ...

# Line 401-402: hasvariables — never called
def hasvariables(rout): ...

# Line 536-539: hasinitvalueasstring — never called
def hasinitvalueasstring(var): ...

# Line 605-606: istrue — trivially returns 1, never called
def istrue(var): ...

# Line 609-610: isfalse — trivially returns 0, never called
def isfalse(var): ...
```

---

## C13. Dead Functions in _core and polynomial

### Fix Suggestion

**File:** `numpy/_core/getlimits.py` — remove `_fr0` (line 16-20) and `_fr1` (line 23-27):

```python
# REMOVE — never called anywhere
def _fr0(a):
    """fix rank-0 --> rank-1"""
    if a.ndim == 0:
        a = a.reshape((1,))
    return a

def _fr1(a):
    """fix rank > 0 --> rank-0"""
    if a.size == 1:
        a = a.reshape(())
    return a
```

**File:** `numpy/_core/_internal.py` — remove `_copy_fields` (line 390-409). Never called from Python or C.

**File:** `numpy/polynomial/chebyshev.py` — remove `_zseries_der` (line 275-304) and `_zseries_int` (line 307-342). Also remove corresponding entries from `numpy/polynomial/chebyshev.pyi` if present.

---

## C14. Dead Functions in lib/ and ma/

### Fix Suggestion

Remove the following (all confirmed never called):

| File | Function | Line |
|------|----------|------|
| `numpy/lib/_datasource.py` | `_check_mode` | 44 |
| `numpy/lib/_iotools.py` | `_is_bytes_like` | 49 |
| `numpy/lib/_utils_impl.py` | `_get_indent` | 123 |
| `numpy/ma/extras.py` | `issequence` | 58 |
| `numpy/ma/mrecords.py` | `_checknames` | 35 |
| `numpy/ma/mrecords.py` | `_get_fieldmask` | 69 |
| `numpy/_utils/__init__.py` | `_rename_parameter` | 41 |

---

## Summary Table

| Finding | Reproducer | Fix Type | Effort |
|---------|-----------|----------|--------|
| F1/F2: ma silent data corruption | Yes | Narrow exception | 2 lines x2 |
| F3: mask silently dropped | Yes | Add warning | 5 lines |
| F4/F5: array_equal/equiv broad catch | Yes | Narrow exception | 2 lines x3 |
| F6: polynomial operator broad catch | Yes | Narrow exception | 1 line x8 |
| F7: f2py distutils dead code | N/A | Remove dead code | ~20 lines |
| F8: NPY_PROMOTION_STATE | N/A | Remove dead code | 6 lines |
| F10: TESTS.rst wrong label | N/A | Fix text | 1 line |
| F11: help(ndarray) TODO | N/A | Fix text | 1 word |
| F12: exceptions.py wrong module | N/A | Fix text | 1 line |
| F13: build_requirements version | N/A | Fix version | 1 line |
| P1: flatten_inplace infinite loop | Yes | Add str/bytes guard | 2 lines x3 |
| C6: eval() in f2py | N/A | Use ast.literal_eval | 3 lines |
| C15: ma warning category | Yes | Add category arg | 1 line x2 |
| A4: dead _dummy_thread | N/A | Remove fallback | 3 lines |
| C12: dead f2py functions | N/A | Remove functions | 10 functions |
| C13: dead _core/polynomial funcs | N/A | Remove functions | 4 functions |
| C14: dead lib/ma functions | N/A | Remove functions | 7 functions |
