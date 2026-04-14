"""Validation test: libfiu against CPython's _zstd extension + libzstd.

This is the first real-world end-to-end test of the libfiu toolchain against
a foreign-C-allocator extension. The target is CPython 3.14's stdlib _zstd
module, which links against libzstd.so.1 and calls ZSTD_createCCtx() from
_zstd.compressor._zstd_ZstdCompressor___init___impl at Modules/_zstd/
compressor.c:354.

ZSTD_createCCtx() internally calls libc malloc to allocate its compression
context. If libfiu's posix preload successfully intercepts that malloc, the
function returns NULL, and CPython's C code raises ZstdError with message
"Unable to create ZSTD_CCtx instance." — that's the success case for this
test.

What we verify:
  1. Baseline: ZstdCompressor() succeeds normally.
  2. Unconditional libc/mm/malloc enable triggers the ZstdError path.
     (A ZstdCompressor() creation is likely to involve more than one malloc,
     so we use enable_if with a shot-once flag rather than unconditional fire.)
  3. Targeted: from_stack_of("ZSTD_createCCtx") fires only for mallocs
     called from within that libzstd function, leaving everything else
     unaffected.
  4. Both failure paths produce ZstdError, not a segfault.
"""
import sys
sys.path.insert(0, "/home/danzin/projects/cext-review-toolkit/docs")
import libfiu_helpers as fh

fh.require_preloaded()

# Promote libzstd symbols into the global namespace so that
# fiu_enable_stack_by_name can resolve ZSTD_createCCtx via dlsym.
# This MUST happen before `import compression.zstd` triggers the
# RTLD_LOCAL dlopen of _zstd.so (which would otherwise keep libzstd's
# symbols invisible to dlsym(RTLD_DEFAULT, ...)).
_libzstd = fh.promote_to_global("libzstd.so.1")

import compression.zstd as czstd


def baseline():
    c = czstd.ZstdCompressor()
    data = c.compress(b"hello " * 100) + c.flush()
    print(f"  baseline: compressed to {len(data)} bytes (expected >0)")
    assert len(data) > 0


def test_predicate_gate():
    """Fail one malloc after a flag is set — coarse test that we can
    reach libzstd's allocator through the LD_PRELOAD chain at all."""
    fire_next = [False]

    with fh.enable_if("libc/mm/malloc", lambda: fire_next[0]) as state:
        # First: compressor construction should succeed.
        c1 = czstd.ZstdCompressor()
        print(f"  preflight: compressor constructed, state.count={state['count']}")
        del c1

        # Now arm the predicate and try again — the first malloc inside
        # this region will return NULL. Whether that breaks ZstdCompressor
        # construction depends on which malloc happens first: if it is the
        # ZSTD_createCCtx internal, we get ZstdError; if it is a Python-side
        # malloc, we get MemoryError. Either outcome proves libfiu reached
        # the allocation path.
        fire_next[0] = True
        try:
            c2 = czstd.ZstdCompressor()
            fire_next[0] = False
            print(f"  FAIL: ZstdCompressor() unexpectedly succeeded "
                  f"(no malloc fired predicate? state.count={state['count']})")
            del c2
        except (czstd.ZstdError, MemoryError) as e:
            fire_next[0] = False
            print(f"  OK: {type(e).__name__}: {e}")
        print(f"  predicate fired {state['failed']} time(s), "
              f"total callbacks {state['count']}")


def test_targeted_zstd_createcctx():
    """The high-value test: fail ONLY mallocs called from inside
    ZSTD_createCCtx, leaving everything else intact.

    Expected: ZstdCompressor() raises ZstdError("Unable to create ZSTD_CCtx
    instance."). Any other exception or a segfault is a failure.
    """
    with fh.from_stack_of("libc/mm/malloc", func_name="ZSTD_createCCtx"):
        try:
            c = czstd.ZstdCompressor()
            print("  FAIL: ZstdCompressor() succeeded — stack targeting "
                  "did not intercept ZSTD_createCCtx (is the symbol in "
                  "libzstd's dynamic symbol table?)")
            del c
        except czstd.ZstdError as e:
            print(f"  OK: ZstdError raised: {e}")
        except MemoryError as e:
            print(f"  UNEXPECTED: MemoryError raised (stack targeting hit "
                  f"a different malloc than expected): {e}")


print("=== 1. Baseline ===")
baseline()

print("\n=== 2. enable_if predicate gate ===")
test_predicate_gate()

print("\n=== 3. from_stack_of ZSTD_createCCtx ===")
test_targeted_zstd_createcctx()

print("\n=== 4. Post-test baseline ===")
baseline()
print("All tests completed without segfault.")
