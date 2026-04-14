"""Reusable helpers for libfiu-based allocation failure reproducers.

See docs/reproducer-techniques.md Technique 23 for the full narrative.

Usage:
    import libfiu_helpers as fh

    # Fail the 3rd system malloc globally:
    with fh.nth_allocation("libc/mm/malloc", n=3):
        do_something()

    # Fail only when a specific C function is on the call stack:
    with fh.from_stack_of("libc/mm/malloc", func_name="ZSTD_createCCtx"):
        do_something()

    # Fail all mallocs matching a predicate (e.g. size >= 1024):
    with fh.enable_if("libc/mm/malloc", lambda: True):  # predicate-less variant
        do_something()

Prerequisites:
  - libfiu installed locally (see docs/reproducer-techniques.md Technique 23)
  - LD_LIBRARY_PATH and LD_PRELOAD set to load fiu_run_preload.so +
    fiu_posix_preload.so before Python starts. Usually:

      export LD_LIBRARY_PATH=$HOME/projects/libfiu/install/lib
      export LD_PRELOAD=$HOME/projects/libfiu/install/lib/fiu_run_preload.so:\\
                        $HOME/projects/libfiu/install/lib/fiu_posix_preload.so

  - The `fiu` Python module installed into the interpreter/venv that runs
    the reproducer (see bindings/python/ in the libfiu source tree).

Caveats:
  - `ctypes.CDLL("libc.so.6")` bypasses LD_PRELOAD because it is a direct
    dlopen. Use `ctypes.CDLL(None)` (RTLD_DEFAULT) when you want the preload
    to win symbol resolution for your own test code.
  - Unconditional `enable("libc/mm/malloc")` under `PYTHONMALLOC=malloc` will
    break the interpreter — even `fiu.disable()` needs to allocate. Always use
    one of the scoped helpers below, never bare `fiu.enable()`.
  - `enable_stack_by_name` uses `dlsym()` to resolve the target function, so
    the function must be in the dynamic symbol table (not `static`, and the
    binary must not be stripped or built with `-fvisibility=hidden` for the
    targeted symbol). For CPython stdlib extensions this is normally fine.
  - Free pymalloc objects ≤ 512 bytes come from arena pools, not `libc/mm/malloc`.
    libfiu will only intercept Python-level allocations that either exceed the
    pymalloc pool size or run under `PYTHONMALLOC=malloc`. For targeting
    specifically named C functions inside a library, `from_stack_of` is usually
    the more reliable primitive.
"""

from __future__ import annotations

import contextlib
from typing import Callable, Iterator

try:
    import fiu
except ImportError as exc:
    raise ImportError(
        "libfiu Python bindings are not installed. Build them from the libfiu "
        "source tree: `cd bindings/python && python setup.py install`. "
        "See docs/reproducer-techniques.md Technique 23."
    ) from exc


@contextlib.contextmanager
def nth_allocation(
    failure_point: str,
    n: int,
    *,
    repeat: bool = False,
) -> Iterator[dict]:
    """Fail the N-th call to ``failure_point`` (1-indexed), then un-hook.

    If ``repeat`` is True, every subsequent call is also failed until the
    context exits. The default is to fail exactly once.

    Yields a state dict with ``count`` (total callbacks fired) and
    ``failed_at`` (list of indices where failure was injected) — useful for
    test assertions.

    Example:
        with nth_allocation("libc/mm/malloc", n=1) as state:
            # First malloc in the protected block returns NULL.
            create_compressor_expecting_failure()
        assert state["failed_at"] == [1]
    """
    state: dict = {"count": 0, "failed_at": [], "target_n": n}

    def predicate(name, *_args) -> int:
        state["count"] += 1
        c = state["count"]
        if c == n or (repeat and c > n):
            state["failed_at"].append(c)
            return 1
        return 0

    fiu.enable_external(failure_point, predicate)
    try:
        yield state
    finally:
        try:
            fiu.disable(failure_point)
        except Exception:
            # Already disabled — acceptable.
            pass


@contextlib.contextmanager
def enable_if(
    failure_point: str,
    predicate: Callable[[], bool],
) -> Iterator[dict]:
    """Fail ``failure_point`` whenever ``predicate()`` returns True.

    The predicate takes no arguments and is called once per allocation.
    Use this for stateful patterns that are not simple N-th-call counting —
    for example, "fail all allocations after a flag is set".

    Yields a state dict with ``count`` (callbacks fired) and ``failed``
    (how many times the predicate returned True).

    Example:
        should_fail = [False]
        with enable_if("libc/mm/malloc", lambda: should_fail[0]) as state:
            setup_phase()               # allocations succeed
            should_fail[0] = True
            exercise_error_path()       # next allocation fails
            should_fail[0] = False
            cleanup_phase()             # allocations succeed again
    """
    state: dict = {"count": 0, "failed": 0}

    def cb(name, *_args) -> int:
        state["count"] += 1
        if predicate():
            state["failed"] += 1
            return 1
        return 0

    fiu.enable_external(failure_point, cb)
    try:
        yield state
    finally:
        try:
            fiu.disable(failure_point)
        except Exception:
            pass


@contextlib.contextmanager
def from_stack_of(
    failure_point: str,
    func_name: str,
    *,
    failnum: int = 1,
    onetime: bool = False,
) -> Iterator[None]:
    """Fail ``failure_point`` only when ``func_name`` is on the call stack.

    This uses libfiu's `fiu_enable_stack_by_name` which walks the backtrace
    via glibc's `backtrace()` and `dladdr()`. Inlined and static-with-hidden-
    visibility functions will NOT appear on the backtrace; the target must
    be in the dynamic symbol table.

    Example:
        # Fail ONLY mallocs called from within ZSTD_createCCtx, leaving the
        # surrounding Python infrastructure's allocations intact.
        with from_stack_of("libc/mm/malloc", "ZSTD_createCCtx"):
            compression.zstd.ZstdCompressor()  # raises ZstdError
    """
    flags = fiu.Flags.ONETIME if onetime else 0
    fiu.enable_stack_by_name(
        failure_point,
        func_name=func_name,
        failnum=failnum,
        flags=flags,
        pos_in_stack=-1,
    )
    try:
        yield
    finally:
        try:
            fiu.disable(failure_point)
        except Exception:
            pass


def promote_to_global(libname: str) -> object:
    """Dlopen ``libname`` with ``RTLD_GLOBAL`` so its symbols become visible
    to ``dlsym(RTLD_DEFAULT, ...)``.

    This is required before using ``from_stack_of(func_name=...)`` against
    a function that lives in a shared library which was loaded indirectly
    (e.g. by a CPython extension module via the default ``RTLD_LOCAL``
    dlopen). Without this, ``fiu_enable_stack_by_name`` returns -1 because
    the dynamic linker cannot resolve the target symbol.

    Example:
        promote_to_global("libzstd.so.1")
        with from_stack_of("libc/mm/malloc", func_name="ZSTD_createCCtx"):
            compression.zstd.ZstdCompressor()

    Returns the ``ctypes.CDLL`` handle (kept alive for the caller's
    convenience).
    """
    import ctypes
    return ctypes.CDLL(libname, mode=ctypes.RTLD_GLOBAL)


def require_preloaded() -> None:
    """Abort loudly if libfiu's POSIX preload isn't actually loaded.

    Call this at the top of a reproducer to avoid the confusing failure mode
    where the test runs without LD_PRELOAD set and every helper silently
    no-ops (because the failure points don't exist without the preload).

    Raises RuntimeError if the POSIX module can't be found in the running
    process's loaded libraries.
    """
    import os
    preload = os.environ.get("LD_PRELOAD", "")
    if "fiu_posix_preload" not in preload:
        raise RuntimeError(
            "libfiu POSIX preload is not in LD_PRELOAD. Set:\n"
            "  export LD_LIBRARY_PATH=$HOME/projects/libfiu/install/lib\n"
            "  export LD_PRELOAD=$HOME/projects/libfiu/install/lib/fiu_run_preload.so:"
            "$HOME/projects/libfiu/install/lib/fiu_posix_preload.so\n"
            "and re-run the reproducer."
        )
