/*
 * mallocfault.c -- LD_PRELOAD malloc/calloc/realloc fault-injection shim.
 *
 * A self-contained, libfiu-free way to drive MemoryError into a C/Cython
 * extension's error paths (the libxml2/libxslt/zlib/etc. allocations a pure
 * Python harness can't reach). See reproducer-techniques.md Technique 31 for
 * usage; Technique 23 covers the libfiu-based alternative when libfiu is
 * installed. First built for the lxml free-threading re-review.
 *
 * The shim starts DISARMED so it never disturbs interpreter startup. It only
 * begins faulting once the test "arms" it. Arming is done out-of-band by the
 * Python test creating a sentinel file (path in env MF_ARM_FILE); the shim
 * stat()s that file on every allocation, so arm/disarm is observable across
 * the C/Python boundary with no shared state beyond the filesystem.
 *
 * Environment variables (all read live on each allocation):
 *   MF_ARM_FILE   path to a sentinel file. Faulting is active only while the
 *                 file exists. Required to do anything; if unset, shim is a
 *                 transparent passthrough.
 *   MF_FAIL_AFTER N   skip the first N *eligible* (armed + size-window) allocs,
 *                 then start failing. Default 0 (fail immediately when armed).
 *   MF_FAIL_EVERY K   once failing, fail 1 of every K eligible allocs.
 *                 Default 1 (fail every eligible alloc).
 *   MF_FAIL_COUNT C   fail at most C allocations total, then pass everything
 *                 (even while armed). Default 1. 0 means unlimited.
 *   MF_MIN_SIZE   only fault allocations with size >= MF_MIN_SIZE. Default 0.
 *   MF_MAX_SIZE   only fault allocations with size <= MF_MAX_SIZE. Default 0
 *                 (no upper bound).
 *   MF_LOG        if set to 1, log each fault to stderr.
 *
 * The "eligible count" is reset to 0 whenever the sentinel file transitions
 * from absent->present (i.e. each fresh arm() starts a new counting window),
 * so MF_FAIL_AFTER targets the Nth eligible alloc *after* the most recent arm.
 *
 * Compile:
 *   gcc -shared -fPIC -O2 -o mallocfault.so mallocfault.c -ldl
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

typedef void *(*malloc_fn)(size_t);
typedef void *(*calloc_fn)(size_t, size_t);
typedef void *(*realloc_fn)(void *, size_t);

static malloc_fn real_malloc = NULL;
static calloc_fn real_calloc = NULL;
static realloc_fn real_realloc = NULL;

/* Counting state. Single-process, single-threaded error-path tests, so plain
 * (non-atomic) statics are fine. */
static long eligible_seen = 0;   /* eligible allocs since last (re)arm */
static long failed_so_far = 0;   /* total faults injected since last (re)arm */
static int  was_armed = 0;       /* sentinel-present state last time we checked */

/* calloc is called by dlsym() on some libc versions during bootstrap; break the
 * recursion with a tiny static buffer used only before real_calloc resolves. */
static char bootstrap_buf[4096];
static size_t bootstrap_off = 0;
static int in_bootstrap = 0;

static void resolve_reals(void) {
    if (real_malloc && real_calloc && real_realloc) return;
    in_bootstrap = 1;
    real_malloc = (malloc_fn)dlsym(RTLD_NEXT, "malloc");
    real_calloc = (calloc_fn)dlsym(RTLD_NEXT, "calloc");
    real_realloc = (realloc_fn)dlsym(RTLD_NEXT, "realloc");
    in_bootstrap = 0;
}

static int env_int(const char *name, long *out) {
    const char *v = getenv(name);
    if (!v || !*v) return 0;
    *out = strtol(v, NULL, 10);
    return 1;
}

/* Return 1 if the sentinel file currently exists (i.e. armed). Also resets the
 * counting window on a fresh arm (absent -> present transition). */
static int currently_armed(void) {
    const char *path = getenv("MF_ARM_FILE");
    if (!path || !*path) return 0;
    struct stat st;
    int armed = (stat(path, &st) == 0);
    if (armed && !was_armed) {
        /* fresh arm: restart counting */
        eligible_seen = 0;
        failed_so_far = 0;
    }
    was_armed = armed;
    return armed;
}

/* Decide whether an allocation of `size` should be faulted right now. */
static int should_fail(size_t size) {
    if (in_bootstrap) return 0;
    if (!currently_armed()) return 0;

    long min_size = 0, max_size = 0;
    env_int("MF_MIN_SIZE", &min_size);
    env_int("MF_MAX_SIZE", &max_size);
    if (min_size > 0 && (long)size < min_size) return 0;
    if (max_size > 0 && (long)size > max_size) return 0;

    long fail_after = 0, fail_every = 1, fail_count = 1;
    env_int("MF_FAIL_AFTER", &fail_after);
    env_int("MF_FAIL_EVERY", &fail_every);
    if (fail_every < 1) fail_every = 1;
    /* MF_FAIL_COUNT default 1; explicit 0 means unlimited. */
    if (!env_int("MF_FAIL_COUNT", &fail_count)) fail_count = 1;

    long idx = eligible_seen;  /* 0-based index of this eligible alloc */
    eligible_seen++;

    if (idx < fail_after) return 0;          /* still in the skip window */
    if ((idx - fail_after) % fail_every != 0) return 0;
    if (fail_count > 0 && failed_so_far >= fail_count) return 0;

    failed_so_far++;
    if (getenv("MF_LOG")) {
        fprintf(stderr,
                "[mallocfault] FAIL alloc idx=%ld size=%zu (failed_so_far=%ld)\n",
                idx, size, failed_so_far);
    }
    return 1;
}

void *malloc(size_t size) {
    if (!real_malloc) {
        resolve_reals();
        if (!real_malloc) {
            /* still bootstrapping: serve from the tiny static buffer */
            if (bootstrap_off + size <= sizeof(bootstrap_buf)) {
                void *p = &bootstrap_buf[bootstrap_off];
                bootstrap_off += size;
                return p;
            }
            return NULL;
        }
    }
    if (should_fail(size)) {
        errno = ENOMEM;
        return NULL;
    }
    return real_malloc(size);
}

void *calloc(size_t nmemb, size_t size) {
    if (!real_calloc) {
        resolve_reals();
        if (!real_calloc) {
            size_t total = nmemb * size;
            if (bootstrap_off + total <= sizeof(bootstrap_buf)) {
                void *p = &bootstrap_buf[bootstrap_off];
                bootstrap_off += total;
                memset(p, 0, total);
                return p;
            }
            return NULL;
        }
    }
    /* guard against size_t overflow producing a tiny window match */
    size_t total = nmemb * size;
    if (nmemb != 0 && total / nmemb != size) total = (size_t)-1;
    if (should_fail(total)) {
        errno = ENOMEM;
        return NULL;
    }
    return real_calloc(nmemb, size);
}

void *realloc(void *ptr, size_t size) {
    if (!real_realloc) {
        resolve_reals();
        if (!real_realloc) return NULL;
    }
    /* Never fault frees-via-realloc (size 0) or bootstrap-buffer pointers. */
    if (size != 0 &&
        !(ptr >= (void *)bootstrap_buf &&
          ptr < (void *)(bootstrap_buf + sizeof(bootstrap_buf)))) {
        if (should_fail(size)) {
            errno = ENOMEM;
            return NULL;
        }
    }
    return real_realloc(ptr, size);
}
