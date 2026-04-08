#!/usr/bin/env python3
"""Find GIL discipline issues in C extension code.

Detects mismatched allow-threads macros, Python API calls without GIL,
blocking calls with GIL held, and free-threading concerns.

Usage:
    python scan_gil_usage.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes_for_file, extract_functions, find_calls_in_scope,
    extract_static_declarations,
    get_node_text, walk_descendants, strip_comments,
)
from scan_common import find_project_root, discover_c_files, parse_common_args


_BLOCKING_CALLS = {
    "read", "write", "recv", "send", "recvfrom", "sendto",
    "sleep", "usleep", "nanosleep",
    "select", "poll", "epoll_wait", "pselect",
    "connect", "accept", "listen", "bind",
    "flock", "lockf", "fcntl",
    "popen", "system", "exec", "execv", "execve",
    "getaddrinfo", "gethostbyname",
    "pthread_mutex_lock", "pthread_cond_wait",
    "sem_wait", "sem_timedwait",
    "fread", "fwrite", "fgets", "fputs",
    "waitpid", "wait",
}


def _check_mismatched_allow_threads(func, source_bytes):
    """Check for mismatched Py_BEGIN/END_ALLOW_THREADS."""
    findings = []
    body_text = strip_comments(func["body"])

    begin_count = body_text.count("Py_BEGIN_ALLOW_THREADS")
    end_count = body_text.count("Py_END_ALLOW_THREADS")

    if begin_count != end_count:
        findings.append({
            "type": "mismatched_allow_threads",
            "file": "",
            "function": func["name"],
            "line": func["start_line"],
            "confidence": "high",
            "detail": (f"Mismatched GIL macros: {begin_count} "
                       f"Py_BEGIN_ALLOW_THREADS vs {end_count} "
                       f"Py_END_ALLOW_THREADS"),
            "begin_count": begin_count,
            "end_count": end_count,
        })

    return findings


def _check_api_without_gil(func, source_bytes):
    """Check for Python API calls between BEGIN/END_ALLOW_THREADS."""
    findings = []
    body_text = func["body"]

    # Find regions between BEGIN and END.
    begin_re = re.compile(r'Py_BEGIN_ALLOW_THREADS')
    end_re = re.compile(r'Py_END_ALLOW_THREADS')

    begins = list(begin_re.finditer(body_text))
    ends = list(end_re.finditer(body_text))

    for b in begins:
        # Find the matching END.
        matching_end = None
        for e in ends:
            if e.start() > b.end():
                matching_end = e
                break
        if not matching_end:
            continue

        region = body_text[b.end():matching_end.start()]
        # Look for Py* or _Py* calls in this region.
        py_calls = re.finditer(r'\b(Py\w+|_Py\w+)\s*\(', region)
        for m in py_calls:
            api_name = m.group(1)
            if api_name in ("Py_BEGIN_ALLOW_THREADS", "Py_END_ALLOW_THREADS"):
                continue
            line_offset = body_text[:b.end() + m.start()].count('\n')
            findings.append({
                "type": "api_without_gil",
                "file": "",
                "function": func["name"],
                "line": func["start_line"] + line_offset,
                "confidence": "high",
                "detail": (f"Python API call {api_name}() in GIL-released "
                           f"region (between Py_BEGIN/END_ALLOW_THREADS)"),
                "api_call": api_name,
            })

    return findings


def _check_blocking_with_gil(func, source_bytes):
    """Check for blocking calls in functions that hold the GIL."""
    findings = []
    body = func["body_node"]
    body_text = func["body"]

    # Only check functions that interact with Python.
    has_python_calls = bool(re.search(r'\bPy\w+\s*\(', body_text))
    if not has_python_calls:
        return findings

    has_gil_release = "Py_BEGIN_ALLOW_THREADS" in body_text

    if has_gil_release:
        return findings

    blocking_calls = find_calls_in_scope(body, source_bytes,
                                          api_names=_BLOCKING_CALLS)
    for call in blocking_calls:
        findings.append({
            "type": "blocking_with_gil",
            "file": "",
            "function": func["name"],
            "line": call["start_line"],
            "confidence": "medium",
            "detail": (f"Blocking call {call['function_name']}() in a "
                       f"function that holds the GIL and never releases it"),
            "blocking_call": call["function_name"],
        })

    return findings


def _check_mismatched_gilstate(func, source_bytes):
    """Check for mismatched PyGILState_Ensure/Release."""
    findings = []
    body_text = func["body"]

    ensure_count = body_text.count("PyGILState_Ensure")
    release_count = body_text.count("PyGILState_Release")

    if ensure_count != release_count and (ensure_count > 0 or release_count > 0):
        findings.append({
            "type": "mismatched_gilstate",
            "file": "",
            "function": func["name"],
            "line": func["start_line"],
            "confidence": "high",
            "detail": (f"Mismatched GIL state: {ensure_count} "
                       f"PyGILState_Ensure vs {release_count} "
                       f"PyGILState_Release"),
        })

    return findings


# CPython type slot field names — functions assigned to these are called by
# CPython with the GIL held and should not be flagged as foreign callbacks.
_TYPE_SLOT_NAMES = {
    # PyTypeObject fields
    "tp_dealloc", "tp_vectorcall_offset", "tp_getattr", "tp_setattr",
    "tp_as_async", "tp_repr", "tp_as_number", "tp_as_sequence",
    "tp_as_mapping", "tp_hash", "tp_call", "tp_str", "tp_getattro",
    "tp_setattro", "tp_as_buffer", "tp_traverse", "tp_clear",
    "tp_richcompare", "tp_iter", "tp_iternext", "tp_methods",
    "tp_members", "tp_getset", "tp_base", "tp_descr_get",
    "tp_descr_set", "tp_init", "tp_alloc", "tp_new", "tp_free",
    "tp_is_gc", "tp_finalize",
    # Number protocol
    "nb_add", "nb_subtract", "nb_multiply", "nb_remainder",
    "nb_divmod", "nb_power", "nb_negative", "nb_positive",
    "nb_absolute", "nb_bool", "nb_invert", "nb_lshift", "nb_rshift",
    "nb_and", "nb_xor", "nb_or", "nb_int", "nb_float",
    "nb_inplace_add", "nb_inplace_subtract", "nb_inplace_multiply",
    "nb_inplace_remainder", "nb_inplace_power", "nb_inplace_lshift",
    "nb_inplace_rshift", "nb_inplace_and", "nb_inplace_xor",
    "nb_inplace_or", "nb_floor_divide", "nb_true_divide",
    "nb_inplace_floor_divide", "nb_inplace_true_divide", "nb_index",
    "nb_matrix_multiply", "nb_inplace_matrix_multiply",
    # Sequence protocol
    "sq_length", "sq_concat", "sq_repeat", "sq_item",
    "sq_ass_item", "sq_contains", "sq_inplace_concat",
    "sq_inplace_repeat",
    # Mapping protocol
    "mp_length", "mp_subscript", "mp_ass_subscript",
    # Buffer protocol
    "bf_getbuffer", "bf_releasebuffer",
    # PyType_Slot slot IDs
    "Py_tp_dealloc", "Py_tp_repr", "Py_tp_hash", "Py_tp_call",
    "Py_tp_str", "Py_tp_getattro", "Py_tp_setattro", "Py_tp_traverse",
    "Py_tp_clear", "Py_tp_richcompare", "Py_tp_iter", "Py_tp_iternext",
    "Py_tp_methods", "Py_tp_members", "Py_tp_getset", "Py_tp_descr_get",
    "Py_tp_descr_set", "Py_tp_init", "Py_tp_alloc", "Py_tp_new",
    "Py_tp_free", "Py_tp_is_gc", "Py_tp_finalize",
    "Py_nb_add", "Py_nb_subtract", "Py_nb_multiply", "Py_nb_bool",
    "Py_nb_int", "Py_nb_float", "Py_nb_index", "Py_nb_negative",
    "Py_nb_positive", "Py_nb_absolute", "Py_nb_invert",
    "Py_sq_length", "Py_sq_concat", "Py_sq_repeat", "Py_sq_item",
    "Py_sq_ass_item", "Py_sq_contains",
    "Py_mp_length", "Py_mp_subscript", "Py_mp_ass_subscript",
    "Py_bf_getbuffer", "Py_bf_releasebuffer",
}


def _find_type_slot_functions(source_bytes: bytes) -> set[str]:
    """Find function names assigned to CPython type slots in struct initializers."""
    source_text = source_bytes.decode("utf-8", errors="replace")
    slot_funcs: set[str] = set()

    # Match designated initializer fields: .tp_dealloc = (destructor)func_name
    for m in re.finditer(
        r'\.(\w+)\s*=\s*(?:\([^)]*\)\s*)?(\w+)',
        source_text,
    ):
        field_name, func_name = m.group(1), m.group(2)
        if field_name in _TYPE_SLOT_NAMES:
            slot_funcs.add(func_name)

    # Match PyType_Slot entries: {Py_tp_dealloc, (destructor)func_name}
    for m in re.finditer(
        r'\{\s*(\w+)\s*,\s*(?:\([^)]*\)\s*)?(\w+)\s*\}',
        source_text,
    ):
        slot_id, func_name = m.group(1), m.group(2)
        if slot_id in _TYPE_SLOT_NAMES:
            slot_funcs.add(func_name)

    return slot_funcs


def _check_callback_without_gil(functions, source_bytes):
    """Check for functions used as callbacks that call Python APIs without GIL."""
    findings = []

    callback_candidates = set()
    all_func_names = {f["name"] for f in functions}

    # Find functions assigned to type slots — these are NOT foreign callbacks.
    type_slot_funcs = _find_type_slot_functions(source_bytes)

    for func in functions:
        body = func["body_node"]
        calls = find_calls_in_scope(body, source_bytes)
        for call in calls:
            fn = call["function_name"]
            if fn.startswith("Py") or fn.startswith("_Py"):
                continue
            args_text = call["arguments_text"]
            for candidate in all_func_names:
                if re.search(r'\b' + re.escape(candidate) + r'\b', args_text):
                    callback_candidates.add(candidate)

    # Exclude functions known to be type slots (called by CPython with GIL held).
    callback_candidates -= type_slot_funcs

    for func in functions:
        if func["name"] not in callback_candidates:
            continue

        body_text = func["body"]
        has_python_calls = bool(re.search(r'\bPy[A-Z]\w+\s*\(', body_text))
        if not has_python_calls:
            continue

        if "PyGILState_Ensure" in body_text:
            continue

        findings.append({
            "type": "callback_without_gil",
            "file": "",
            "function": func["name"],
            "line": func["start_line"],
            "confidence": "medium",
            "detail": (f"Function '{func['name']}' appears to be used as a "
                       f"callback to a foreign library and calls Python APIs "
                       f"without PyGILState_Ensure"),
        })

    return findings


def _check_free_threading(tree, source_bytes):
    """Check for free-threading concerns (static mutable state, missing Py_mod_gil)."""
    findings = []

    statics = extract_static_declarations(tree, source_bytes)
    mutable_statics = [
        s for s in statics
        if not s["is_const"] and s["is_pyobject"]
    ]

    for s in mutable_statics:
        findings.append({
            "type": "free_threading_concern",
            "file": "",
            "function": "(file scope)",
            "line": s["start_line"],
            "confidence": "medium",
            "detail": (f"Static mutable PyObject* '{s['name']}' is not "
                       f"thread-safe for free-threaded Python (PEP 703)"),
            "variable": s["name"],
        })

    # Check for Py_mod_gil slot.
    body_text = source_bytes.decode("utf-8", errors="replace")
    has_mod_slots = "PyModuleDef_Slot" in body_text
    has_mod_gil = "Py_mod_gil" in body_text

    if has_mod_slots and not has_mod_gil:
        findings.append({
            "type": "free_threading_concern",
            "file": "",
            "function": "(module definition)",
            "line": 0,
            "confidence": "low",
            "detail": "Module uses PyModuleDef_Slot but lacks Py_mod_gil slot",
        })

    return findings


def _check_object_invalidation(func, source_bytes):
    """Check for self->member use after GIL release/reacquire.

    When the GIL is released, another thread could call close() or
    mutate the object's state. After reacquiring the GIL, code that
    uses self->member without re-validation is operating on potentially
    stale/invalid state.
    """
    findings = []
    body_text = func["body"]

    begin_re = re.compile(r"Py_BEGIN_ALLOW_THREADS")
    end_re = re.compile(r"Py_END_ALLOW_THREADS")

    begins = list(begin_re.finditer(body_text))
    ends = list(end_re.finditer(body_text))

    for b in begins:
        matching_end = None
        for e in ends:
            if e.start() > b.end():
                matching_end = e
                break
        if not matching_end:
            continue

        # Get the code AFTER Py_END_ALLOW_THREADS (post-reacquire region)
        post_region = body_text[matching_end.end():]

        # Find self->member accesses in the post-reacquire region
        member_accesses = re.finditer(
            r"\bself\s*->\s*(\w+)", post_region
        )
        for m in member_accesses:
            member = m.group(1)
            # Check if this member was also accessed BEFORE the GIL release
            pre_region = body_text[:b.start()]
            if re.search(rf"\bself\s*->\s*{re.escape(member)}\b", pre_region):
                line_offset = body_text[:matching_end.end() + m.start()].count("\n")
                findings.append({
                    "type": "object_invalidation_across_gil_release",
                    "file": "",
                    "function": func["name"],
                    "line": func["start_line"] + line_offset,
                    "confidence": "medium",
                    "detail": (
                        f"self->{member} used after GIL reacquire "
                        f"(another thread could have invalidated it "
                        f"during Py_BEGIN/END_ALLOW_THREADS)"
                    ),
                    "member": member,
                })
                break  # One finding per GIL-release region

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for GIL discipline issues."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    findings = []
    total_functions = 0
    files_analyzed = 0
    skipped = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        for func in functions:
            total_functions += 1
            for checker in (_check_mismatched_allow_threads,
                            _check_api_without_gil,
                            _check_blocking_with_gil,
                            _check_mismatched_gilstate,
                            _check_object_invalidation):
                for f in checker(func, source_bytes):
                    f["file"] = rel
                    findings.append(f)

        # File-level checks.
        for f in _check_callback_without_gil(functions, source_bytes):
            f["file"] = rel
            findings.append(f)
        for f in _check_free_threading(tree, source_bytes):
            f["file"] = rel
            findings.append(f)

    by_type = defaultdict(int)
    by_confidence = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_confidence[f["confidence"]] += 1

    result = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "functions_analyzed": total_functions,
        "files_analyzed": files_analyzed,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
    }
    result["skipped_files"] = skipped
    return result


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
