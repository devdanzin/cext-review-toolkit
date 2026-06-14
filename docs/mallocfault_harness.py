"""Reusable harness for libfiu-free malloc-fault OOM / error-path reproducers.

Pairs with ``docs/mallocfault.c`` (the armable allocation-fault ``LD_PRELOAD``
shim) and, optionally, an event-arming counter like ``docs/libxmlcount.c``. See
``docs/reproducer-techniques.md`` Technique 31 for the full method, and
Technique 23 for the libfiu-based alternative.

Build the shims once into this directory before use::

    gcc -shared -fPIC -O2 -o docs/mallocfault.so docs/mallocfault.c -ldl
    gcc -shared -fPIC -O2 -o docs/libxmlcount.so docs/libxmlcount.c -ldl  # if used

Two roles:

1. *Driver* (parent process): ``run_isolated(child_path, ...)`` spawns the
   reproducer's ``child`` entry point in a fresh subprocess under
   ``LD_PRELOAD=mallocfault.so`` with a timeout. A reproduced deadlock shows up
   as ``timed_out=True``; a crash is contained in the child.

2. *Child helper* (inside the ``LD_PRELOAD``'d subprocess): ``arm()`` /
   ``disarm()`` create/remove the sentinel file the shim ``stat()``s, so the
   fault is active only around the targeted call; ``leak_probe()`` measures RSS
   growth across iterations; ``deadlock_probe()`` runs a callable in a watchdog
   thread and reports whether it wedged.

The shim starts disarmed so interpreter startup is never disturbed.

Note: with the event-arming counter and/or to fault small allocations that
otherwise route through pymalloc's arenas, run the child with
``PYTHONMALLOC=malloc`` so those allocations reach the interposable system
allocator.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "mallocfault.so"
COUNT_SHIM = (
    HERE / "libxmlcount.so"
)  # optional event-arming counter (adapt per library)


# --------------------------------------------------------------------------
# Driver side (parent process)
# --------------------------------------------------------------------------
def run_isolated(
    child_path: str,
    *,
    python: str | None = None,
    extra_pythonpath: str | None = None,
    mf_env: dict[str, str] | None = None,
    timeout: float = 15.0,
    use_shim: bool = True,
    use_counter: bool = False,
    arm_file: str | None = None,
) -> dict:
    """Run ``python child_path child`` in an isolated subprocess under the shim.

    Args:
        child_path: path to the reproducer script (it must dispatch on
            ``sys.argv[1] == "child"`` to its ``child()`` entry point).
        python: interpreter to use (default: the current ``sys.executable``;
            pass a free-threaded / TSan interpreter when relevant).
        extra_pythonpath: prepended to ``PYTHONPATH`` (e.g. the extension's
            in-place ``src`` dir).
        use_counter: also preload the event-arming counter shim
            (prints create/free/leaked counts to stderr at exit).

    Returns a dict with ``rc``, ``timed_out``, ``stdout``, ``stderr``.
    ``timed_out=True`` is the signature of a reproduced deadlock.
    """
    interp = python or sys.executable
    env = dict(os.environ)
    if extra_pythonpath:
        env["PYTHONPATH"] = extra_pythonpath + os.pathsep + env.get("PYTHONPATH", "")
    if arm_file is None:
        arm_file = f"/tmp/mf_arm_{os.getpid()}_{Path(child_path).stem}"
    env["MF_ARM_FILE"] = arm_file
    try:  # make sure we start disarmed
        os.unlink(arm_file)
    except FileNotFoundError:
        pass
    if mf_env:
        env.update(mf_env)
    preloads = []
    if use_shim:
        preloads.append(str(SHIM))
    if use_counter:
        preloads.append(str(COUNT_SHIM))
    if preloads:
        env["LD_PRELOAD"] = os.pathsep.join(preloads)

    proc = subprocess.Popen(
        [interp, "-u", child_path, "child"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        out, err = proc.communicate()
    finally:
        try:
            os.unlink(arm_file)
        except FileNotFoundError:
            pass
    return {"rc": proc.returncode, "timed_out": timed_out, "stdout": out, "stderr": err}


# --------------------------------------------------------------------------
# Child side (inside the LD_PRELOAD'd subprocess)
# --------------------------------------------------------------------------
def _arm_file() -> str:
    path = os.environ.get("MF_ARM_FILE")
    if not path:
        raise RuntimeError("MF_ARM_FILE not set; run via run_isolated()")
    return path


def arm() -> None:
    """Arm the malloc-fault shim (create the sentinel the shim ``stat()``s)."""
    fd = os.open(_arm_file(), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)


def disarm() -> None:
    """Disarm the shim (remove the sentinel)."""
    try:
        os.unlink(_arm_file())
    except FileNotFoundError:
        pass


_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def rss_bytes() -> int:
    """Resident set size of this process in bytes (Linux ``/proc/self/statm``)."""
    with open("/proc/self/statm") as fh:
        resident_pages = int(fh.read().split()[1])
    return resident_pages * _PAGE


def leak_probe(
    make_one_leak,
    *,
    warmup: int = 200,
    iters: int = 4000,
    sample_every: int = 500,
) -> dict:
    """Call ``make_one_leak()`` many times; report RSS growth.

    ``make_one_leak()`` must perform exactly one armed-fault leak attempt (arm,
    trigger the faulting op catching the exception, disarm). Monotonic RSS
    growth across iterations is the evidence for a real C-resource leak (Python
    GC cannot reclaim a leaked C-library handle).
    """
    import gc

    for _ in range(warmup):  # let allocator arenas / caches stabilize
        make_one_leak()
    gc.collect()
    start = rss_bytes()
    samples = [(0, start)]
    for i in range(1, iters + 1):
        make_one_leak()
        if i % sample_every == 0:
            gc.collect()
            samples.append((i, rss_bytes()))
    end = rss_bytes()
    return {
        "start": start,
        "end": end,
        "delta": end - start,
        "iters": iters,
        "samples": samples,
        "bytes_per_iter": (end - start) / iters if iters else 0.0,
    }


def deadlock_probe(fn, timeout: float = 6.0) -> bool:
    """Run ``fn`` in a watchdog thread.

    Returns True if it FAILED to finish within ``timeout`` (i.e. it deadlocked
    on a wedged lock), False if it returned or raised normally. Run the post-
    fault op from a *second* thread when the lock is reentrant per-thread: a
    leaked write lock does not block the thread that leaked it, only others.
    """
    done = threading.Event()
    err: list[BaseException] = []

    def runner():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 - we only need a completion signal
            err.append(e)
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    finished = done.wait(timeout)
    if not finished:
        return True  # deadlocked
    if err:
        print(f"  (probe completed by raising {type(err[0]).__name__}: {err[0]})")
    return False
