/*
 * libxmlcount.c -- WORKED EXAMPLE of an LD_PRELOAD "event-arming counter":
 * it COUNTS create/free pairs for specific C library objects to prove handle
 * leaks directly (independent of RSS, which cannot see a small fixed-size
 * leaked struct that the allocator recycles), AND flips the mallocfault.so
 * sentinel at a precise call boundary so a fault lands on a resource buried
 * inside a multi-allocation routine that a raw allocation index can't isolate.
 *
 * This copy targets libxml2/libxslt (the lxml re-review). ADAPT IT to your
 * target: replace the wrapped symbols below with your library's create/free
 * pairs, and the MF_ARM_ON_* event hooks with the call boundaries you need.
 * See reproducer-techniques.md Technique 31.
 *
 * Interposed pairs (example):
 *   xmlSchemaNewDocParserCtxt / xmlSchemaNewParserCtxt  vs  xmlSchemaFreeParserCtxt
 *   xsltNewTransformContext                             vs  xsltFreeTransformContext
 *
 * On process exit, prints the counts to stderr:
 *   [libxmlcount] schema_parser_ctxt: new=N free=M leaked=N-M
 *   [libxmlcount] xslt_transform_ctxt: new=N free=M leaked=N-M
 *
 * A positive `leaked` is direct evidence that a context object was allocated
 * but never freed.
 *
 * Stack with the malloc-fault shim:
 *   LD_PRELOAD=mallocfault.so:libxmlcount.so
 *
 * Compile:
 *   gcc -shared -fPIC -O2 -o libxmlcount.so libxmlcount.c -ldl
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* Opaque pointer types -- we only ever pass them through. */
typedef void *(*new_doc_parser_fn)(void *);
typedef void *(*new_parser_fn)(const char *);
typedef void (*free_parser_fn)(void *);
typedef void *(*xslt_new_fn)(void *, void *);
typedef void (*xslt_free_fn)(void *);
typedef void *(*new_valid_fn)(void *);
typedef void (*free_doc_fn)(void *);

static new_doc_parser_fn real_new_doc_parser = NULL;
static new_parser_fn real_new_parser = NULL;
static free_parser_fn real_free_parser = NULL;
static new_valid_fn real_new_valid = NULL;
static free_doc_fn real_free_doc = NULL;
static xslt_new_fn real_xslt_new = NULL;
static xslt_free_fn real_xslt_free = NULL;

static long schema_new = 0, schema_free = 0;
static long xslt_new = 0, xslt_free = 0;
static long doc_free = 0;

/* Resolve the REAL symbols. RTLD_NEXT does not work from an LD_PRELOAD object
 * for symbols that live in libraries loaded later as dependencies of a dlopen'd
 * module (libxml2/libxslt are pulled in by Python importing etree.so). So we
 * dlopen the sonames explicitly (already mapped + refcounted) and dlsym from
 * those handles. */
static void *xml_handle = NULL;
static void *xslt_handle = NULL;

static void *xml_lib(void) {
    if (!xml_handle) xml_handle = dlopen("libxml2.so.16", RTLD_NOW | RTLD_NOLOAD);
    if (!xml_handle) xml_handle = dlopen("libxml2.so", RTLD_NOW | RTLD_NOLOAD);
    if (!xml_handle) xml_handle = dlopen("libxml2.so.16", RTLD_NOW);
    return xml_handle;
}

static void *xslt_lib(void) {
    if (!xslt_handle) xslt_handle = dlopen("libxslt.so.1", RTLD_NOW | RTLD_NOLOAD);
    if (!xslt_handle) xslt_handle = dlopen("libxslt.so", RTLD_NOW | RTLD_NOLOAD);
    if (!xslt_handle) xslt_handle = dlopen("libxslt.so.1", RTLD_NOW);
    return xslt_handle;
}

__attribute__((destructor)) static void report(void) {
    fprintf(stderr,
            "[libxmlcount] schema_parser_ctxt: new=%ld free=%ld leaked=%ld\n",
            schema_new, schema_free, schema_new - schema_free);
    fprintf(stderr,
            "[libxmlcount] xslt_transform_ctxt: new=%ld free=%ld leaked=%ld\n",
            xslt_new, xslt_free, xslt_new - xslt_free);
    fprintf(stderr, "[libxmlcount] xmlFreeDoc_calls: %ld\n", doc_free);
}

void *xmlSchemaNewDocParserCtxt(void *doc) {
    if (!real_new_doc_parser)
        real_new_doc_parser =
            (new_doc_parser_fn)dlsym(xml_lib(), "xmlSchemaNewDocParserCtxt");
    void *r = real_new_doc_parser(doc);
    if (r) __atomic_fetch_add(&schema_new, 1, __ATOMIC_RELAXED);
    return r;
}

void *xmlSchemaNewParserCtxt(const char *url) {
    if (!real_new_parser)
        real_new_parser = (new_parser_fn)dlsym(xml_lib(), "xmlSchemaNewParserCtxt");
    void *r = real_new_parser(url);
    if (r) __atomic_fetch_add(&schema_new, 1, __ATOMIC_RELAXED);
    return r;
}

void xmlSchemaFreeParserCtxt(void *ctxt) {
    if (!real_free_parser)
        real_free_parser =
            (free_parser_fn)dlsym(xml_lib(), "xmlSchemaFreeParserCtxt");
    if (ctxt) __atomic_fetch_add(&schema_free, 1, __ATOMIC_RELAXED);
    real_free_parser(ctxt);
}

/* Event-arming hook for finding #7: when MF_ARM_ON_XSLT_NEW is set, create the
 * mallocfault sentinel (MF_ARM_FILE) right AFTER a successful
 * xsltNewTransformContext, and remove it on xsltFreeTransformContext. This makes
 * the malloc-fault shim begin faulting exactly in the window where the context
 * is allocated but not yet owned by the RAII cleanup -- i.e. the leak window
 * (xslt.pxi:583 self._context._copy()). It lets us target a window that a raw
 * allocation index cannot isolate (xsltNewTransformContext itself makes hundreds
 * of allocations). */
static void sentinel_create(void) {
    const char *path = getenv("MF_ARM_FILE");
    if (!path || !*path) return;
    int fd = open(path, O_CREAT | O_WRONLY, 0600);
    if (fd >= 0) close(fd);
}

static void sentinel_remove(void) {
    const char *path = getenv("MF_ARM_FILE");
    if (!path || !*path) return;
    unlink(path);
}

static void event_arm(void) {
    if (getenv("MF_ARM_ON_XSLT_NEW")) sentinel_create();
}

static void event_disarm(void) {
    if (getenv("MF_ARM_ON_XSLT_NEW")) sentinel_remove();
}

/* For finding #9: arm the malloc-fault shim right before xmlSchemaNewValidCtxt
 * (called from _ParserSchemaValidationContext.connect, xmlschema.pxi:207, which
 * runs inside _ParserContext.prepare() AFTER the reentrancy PyMutex is acquired
 * at parser.pxi:725). Faulting here makes connect() raise -> prepare() raises with
 * the mutex still held -> the lock leaks (cleanup() is never called because
 * prepare() is outside the caller's try/finally). MF_ARM_ON_VALID_NEW=1 enables. */
void *xmlSchemaNewValidCtxt(void *schema) {
    if (!real_new_valid)
        real_new_valid = (new_valid_fn)dlsym(xml_lib(), "xmlSchemaNewValidCtxt");
    if (getenv("MF_ARM_ON_VALID_NEW")) sentinel_create();
    void *r = real_new_valid(schema);
    return r;
}

/* Count xmlFreeDoc calls. For finding #18, a faulted run that frees FEWER docs
 * than the clean control indicates parsed xmlDoc* results that leaked (the
 * free_doc path in _handleParseResult, parser.pxi:873-901, was skipped by a
 * raise at :890-901). */
void xmlFreeDoc(void *doc) {
    if (!real_free_doc)
        real_free_doc = (free_doc_fn)dlsym(xml_lib(), "xmlFreeDoc");
    if (doc) __atomic_fetch_add(&doc_free, 1, __ATOMIC_RELAXED);
    real_free_doc(doc);
}

/* For finding #18: arm the malloc-fault shim the instant a parse returns a
 * non-NULL xmlDoc (i.e. a well-formed result), so the *next* allocation -- which
 * falls in the unprotected post-parse window of _handleParseResult
 * (parser.pxi:885-901, e.g. the URL/encoding xmlStrdup at :891-894 or any Cython
 * temporary) -- fails. If that window has no try/finally around `result`, the
 * parsed doc leaks. MF_ARM_ON_READ_MEMORY=1 enables. */
typedef void *(*read_mem_fn)(void *, const char *, int, const char *, const char *, int);
static read_mem_fn real_read_mem = NULL;

void *xmlCtxtReadMemory(void *ctxt, const char *buf, int size,
                        const char *url, const char *enc, int options) {
    if (!real_read_mem)
        real_read_mem = (read_mem_fn)dlsym(xml_lib(), "xmlCtxtReadMemory");
    void *r = real_read_mem(ctxt, buf, size, url, enc, options);
    if (r && getenv("MF_ARM_ON_READ_MEMORY")) sentinel_create();
    return r;
}

void *xsltNewTransformContext(void *style, void *doc) {
    if (!real_xslt_new)
        real_xslt_new = (xslt_new_fn)dlsym(xslt_lib(), "xsltNewTransformContext");
    void *r = real_xslt_new(style, doc);
    if (r) {
        __atomic_fetch_add(&xslt_new, 1, __ATOMIC_RELAXED);
        event_arm();  /* start faulting immediately after the ctxt exists */
    }
    return r;
}

void xsltFreeTransformContext(void *ctxt) {
    if (!real_xslt_free)
        real_xslt_free = (xslt_free_fn)dlsym(xslt_lib(), "xsltFreeTransformContext");
    if (ctxt) __atomic_fetch_add(&xslt_free, 1, __ATOMIC_RELAXED);
    event_disarm();  /* stop faulting once the ctxt is (correctly) freed */
    real_xslt_free(ctxt);
}
