#!/usr/bin/env python3
"""Analyze type definitions for correctness in C extension code.

Detects dealloc issues, traverse gaps, richcompare bugs, missing flags,
and type spec problems.

Usage:
    python scan_type_slots.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes_for_file,
    extract_functions,
    extract_struct_initializers,
    find_struct_members,
    strip_comments,
)
from scan_common import find_project_root, discover_c_files, parse_common_args


def _find_func_by_name(functions: list[dict], name: str) -> dict | None:
    """Find a function definition by name."""
    for f in functions:
        if f["name"] == name:
            return f
    return None


def _parse_type_fields(init_text: str) -> dict[str, str]:
    """Parse PyTypeObject designated initializer fields."""
    fields = {}
    for m in re.finditer(r"\.(\w+)\s*=\s*([^,}]+)", init_text):
        fields[m.group(1)] = m.group(2).strip()
    return fields


def _parse_type_slots_array(init_text: str) -> list[tuple[str, str]]:
    """Parse PyType_Slot array entries like {Py_tp_dealloc, func}."""
    slots = []
    for m in re.finditer(r"\{\s*(\w+)\s*,\s*([^}]+)\}", init_text):
        slots.append((m.group(1).strip(), m.group(2).strip()))
    return slots


def _get_struct_name_from_type(
    type_fields: dict[str, str], source_text: str
) -> str | None:
    """Try to extract the C struct name from tp_basicsize = sizeof(StructName)."""
    basicsize = type_fields.get("tp_basicsize", "")
    m = re.search(r"sizeof\s*\(\s*(\w+)\s*\)", basicsize)
    if m:
        return m.group(1)
    return None


def _check_dealloc(
    type_info: dict, functions: list[dict], source_bytes: bytes, source_text: str
):
    """Check tp_dealloc function for correctness."""
    findings = []
    dealloc_name = type_info.get("dealloc_func")
    if not dealloc_name:
        return findings

    # Strip casts like (destructor).
    dealloc_name = re.sub(r"\([^)]*\)\s*", "", dealloc_name).strip()
    dealloc_func = _find_func_by_name(functions, dealloc_name)
    if not dealloc_func:
        return findings

    body_text = strip_comments(dealloc_func["body"])
    has_gc = type_info.get("has_gc", False)

    # Check for tp_free call (strip comments to avoid false positives).
    has_tp_free = "tp_free" in body_text
    has_pyobject_del = "PyObject_Del" in body_text or "PyObject_Free" in body_text

    if not has_tp_free and not has_pyobject_del:
        findings.append(
            {
                "type": "dealloc_missing_tp_free",
                "file": "",
                "function": dealloc_name,
                "line": dealloc_func["start_line"],
                "confidence": "high",
                "detail": (
                    f"tp_dealloc '{dealloc_name}' doesn't call tp_free "
                    f"or any free function — memory leak"
                ),
                "type_name": type_info["name"],
            }
        )

    if has_pyobject_del and not has_tp_free:
        findings.append(
            {
                "type": "dealloc_wrong_free",
                "file": "",
                "function": dealloc_name,
                "line": dealloc_func["start_line"],
                "confidence": "medium",
                "detail": (
                    f"tp_dealloc '{dealloc_name}' uses PyObject_Del/Free "
                    f"instead of Py_TYPE(self)->tp_free — breaks inheritance"
                ),
                "type_name": type_info["name"],
            }
        )

    # Check for GC untrack (body_text already has comments stripped).
    if has_gc and "PyObject_GC_UnTrack" not in body_text:
        findings.append(
            {
                "type": "dealloc_missing_untrack",
                "file": "",
                "function": dealloc_name,
                "line": dealloc_func["start_line"],
                "confidence": "high",
                "detail": (
                    f"Type has Py_TPFLAGS_HAVE_GC but tp_dealloc "
                    f"'{dealloc_name}' doesn't call "
                    f"PyObject_GC_UnTrack"
                ),
                "type_name": type_info["name"],
            }
        )

    # Check for heap type DECREF.
    if type_info.get("is_heap_type", False):
        has_type_decref = (
            "Py_DECREF(Py_TYPE(" in body_text
            or "Py_XDECREF(Py_TYPE(" in body_text
            or ("tp = Py_TYPE(" in body_text and "Py_DECREF(tp)" in body_text)
            or ("type = Py_TYPE(" in body_text and "Py_DECREF(type)" in body_text)
        )
        if not has_type_decref:
            findings.append(
                {
                    "type": "heap_type_missing_type_decref",
                    "file": "",
                    "function": dealloc_name,
                    "line": dealloc_func["start_line"],
                    "confidence": "medium",
                    "detail": (
                        f"Heap type tp_dealloc '{dealloc_name}' doesn't "
                        f"Py_DECREF(Py_TYPE(self)) — type object leak"
                    ),
                    "type_name": type_info["name"],
                }
            )

    return findings


def _check_dealloc_completeness(
    type_info: dict, functions: list[dict], tree, source_bytes: bytes
):
    """Check that dealloc XDECREF's all PyObject* struct members."""
    findings = []
    dealloc_name = type_info.get("dealloc_func")
    struct_name = type_info.get("struct_name")

    if not dealloc_name or not struct_name:
        return findings

    dealloc_name = re.sub(r"\([^)]*\)\s*", "", dealloc_name).strip()
    dealloc_func = _find_func_by_name(functions, dealloc_name)
    if not dealloc_func:
        return findings

    members = find_struct_members(tree, source_bytes, struct_name)
    pyobj_members = [m for m in members if m["is_pyobject"]]

    if not pyobj_members:
        return findings

    dealloc_body = strip_comments(dealloc_func["body"])

    for member in pyobj_members:
        name = member["name"]
        patterns = [
            rf"Py_XDECREF\s*\([^)]*->\s*{re.escape(name)}\s*\)",
            rf"Py_DECREF\s*\([^)]*->\s*{re.escape(name)}\s*\)",
            rf"Py_CLEAR\s*\([^)]*->\s*{re.escape(name)}\s*\)",
            rf"Py_SETREF\s*\([^)]*->\s*{re.escape(name)}\s*,",
        ]
        cleaned = any(re.search(p, dealloc_body) for p in patterns)

        if not cleaned:
            findings.append(
                {
                    "type": "dealloc_missing_xdecref",
                    "file": "",
                    "function": dealloc_name,
                    "line": dealloc_func["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"tp_dealloc '{dealloc_name}' doesn't XDECREF/CLEAR "
                        f"PyObject* member '{name}' of {struct_name} — "
                        f"reference leak per object destruction"
                    ),
                    "type_name": type_info["name"],
                    "missing_member": name,
                }
            )

    return findings


def _check_traverse(type_info: dict, functions: list[dict], tree, source_bytes: bytes):
    """Check tp_traverse visits all PyObject* members."""
    findings = []
    traverse_name = type_info.get("traverse_func")
    struct_name = type_info.get("struct_name")

    if not traverse_name or not struct_name:
        return findings

    traverse_name = re.sub(r"\([^)]*\)\s*", "", traverse_name).strip()
    traverse_func = _find_func_by_name(functions, traverse_name)
    if not traverse_func:
        return findings

    # Find struct members.
    members = find_struct_members(tree, source_bytes, struct_name)
    pyobj_members = [m for m in members if m["is_pyobject"]]

    if not pyobj_members:
        return findings

    traverse_body = strip_comments(traverse_func["body"])
    for member in pyobj_members:
        # Check for Py_VISIT(self->member) or Py_VISIT(member).
        pattern = rf"Py_VISIT\s*\([^)]*{re.escape(member['name'])}"
        if not re.search(pattern, traverse_body):
            findings.append(
                {
                    "type": "traverse_missing_member",
                    "file": "",
                    "function": traverse_name,
                    "line": traverse_func["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"tp_traverse '{traverse_name}' doesn't visit "
                        f"PyObject* member '{member['name']}' of "
                        f"{struct_name}"
                    ),
                    "type_name": type_info["name"],
                    "missing_member": member["name"],
                }
            )

    return findings


def _check_richcompare(type_info: dict, functions: list[dict], source_bytes: bytes):
    """Check tp_richcompare for Py_NotImplemented handling."""
    findings = []
    rc_name = type_info.get("richcompare_func")
    if not rc_name:
        return findings

    rc_name = re.sub(r"\([^)]*\)\s*", "", rc_name).strip()
    rc_func = _find_func_by_name(functions, rc_name)
    if not rc_func:
        return findings

    body = rc_func["body"]
    if "Py_NotImplemented" not in body:
        return findings

    # Check for return Py_NotImplemented without Py_INCREF or Py_RETURN_NOTIMPLEMENTED.
    if "Py_RETURN_NOTIMPLEMENTED" in body:
        return findings

    if "return Py_NotImplemented" in body:
        if (
            "Py_INCREF(Py_NotImplemented)" not in body
            and "Py_NewRef(Py_NotImplemented)" not in body
        ):
            findings.append(
                {
                    "type": "richcompare_not_incref_notimplemented",
                    "file": "",
                    "function": rc_name,
                    "line": rc_func["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"tp_richcompare '{rc_name}' returns "
                        f"Py_NotImplemented without Py_INCREF — "
                        f"use Py_RETURN_NOTIMPLEMENTED"
                    ),
                    "type_name": type_info["name"],
                }
            )

    return findings


def _check_init_reinit_safety(
    type_info: dict, functions: list[dict], tree, source_bytes: bytes
):
    """Check if tp_init is safe to call multiple times.

    Python allows calling __init__ multiple times on the same object.
    If tp_init allocates resources without first cleaning up existing state,
    the second call leaks or corrupts the first call's resources.
    """
    findings = []
    init_name = type_info.get("init_func")
    struct_name = type_info.get("struct_name")

    if not init_name or not struct_name:
        return findings

    init_name = re.sub(r"\([^)]*\)\s*", "", init_name).strip()
    init_func = _find_func_by_name(functions, init_name)
    if not init_func:
        return findings

    body = strip_comments(init_func["body"])

    # Find struct members that are pointers (PyObject* or raw pointers).
    members = find_struct_members(tree, source_bytes, struct_name)
    ptr_members = [m for m in members if m["is_pyobject"] or m["is_pointer"]]

    if not ptr_members:
        return findings

    # Check for allocation/assignment patterns to pointer members.
    # These indicate tp_init sets up resources that would leak on re-init.
    alloc_patterns = [
        r"PyMem_Malloc",
        r"PyMem_Calloc",
        r"PyMem_Realloc",
        r"malloc\s*\(",
        r"calloc\s*\(",
        r"realloc\s*\(",
        r"PyObject_New\b",
        r"PyObject_GC_New\b",
        r"PyList_New",
        r"PyDict_New",
        r"PyTuple_New",
        r"PySet_New",
        r"PyUnicode_FromString",
        r"PyBytes_FromString",
        r"Py_BuildValue",
        r"PyObject_Call",
    ]

    has_alloc = any(re.search(p, body) for p in alloc_patterns)
    if not has_alloc:
        return findings

    # Check if the function guards against re-init.
    # Common patterns: checking if a member is already set, or raising
    # an error if already initialized.
    reinit_guard_patterns = [
        r"already.{0,20}init",  # "already initialized" error
        r"cannot.{0,20}re.?init",  # "cannot reinitialize"
        r"PREVENT_INIT",  # macro-based guard (e.g., APSW)
        r"init_was_called",  # flag-based guard
        r"initialized\b",  # self->initialized flag check
        r"if\s*\(\s*self->\w+\s*!=\s*NULL\s*\)",  # if (self->member != NULL)
        r"if\s*\(\s*self->\w+\s*\)\s*\{[^}]*Py_CLEAR",  # if (self->m) { Py_CLEAR
        r"if\s*\(\s*self->\w+\s*\)\s*\{[^}]*Py_XDECREF",
        r"if\s*\(\s*self->\w+\s*\)\s*\{[^}]*Py_DECREF",
        r"if\s*\(\s*self->\w+\s*\)\s*\{[^}]*free\s*\(",
        r"if\s*\(\s*self->\w+\s*\)\s*\{[^}]*PyMem_Free",
    ]

    has_guard = any(re.search(p, body, re.IGNORECASE) for p in reinit_guard_patterns)
    if has_guard:
        return findings

    # tp_init allocates resources without any re-init guard.
    # Identify which members are assigned.
    assigned_members = []
    for m in ptr_members:
        assign_pattern = rf"self\s*->\s*{re.escape(m['name'])}\s*="
        if re.search(assign_pattern, body):
            assigned_members.append(m["name"])

    if not assigned_members:
        return findings

    findings.append(
        {
            "type": "init_not_reinit_safe",
            "file": "",
            "function": init_name,
            "line": init_func["start_line"],
            "confidence": "medium",
            "detail": (
                f"tp_init '{init_name}' allocates resources and assigns to "
                f"member(s) {', '.join(assigned_members)} without checking "
                f"for prior initialization. A second __init__() call on the "
                f"same object will leak the first call's resources."
            ),
            "type_name": type_info["name"],
        }
    )

    return findings


def _check_new_without_init(
    type_info: dict, functions: list[dict], tree, source_bytes: bytes
):
    """Check if tp_new initializes pointer members to safe defaults.

    Python allows calling tp_new without tp_init (e.g., via
    object.__new__(MyType)). If tp_new doesn't zero pointer members,
    methods may dereference uninitialized pointers.
    """
    findings = []
    new_name = type_info.get("new_func")
    struct_name = type_info.get("struct_name")

    if not new_name or not struct_name:
        return findings

    new_name = re.sub(r"\([^)]*\)\s*", "", new_name).strip()
    new_func = _find_func_by_name(functions, new_name)
    if not new_func:
        return findings

    body = strip_comments(new_func["body"])

    # Find pointer members in the struct.
    members = find_struct_members(tree, source_bytes, struct_name)
    ptr_members = [m for m in members if m["is_pyobject"] or m["is_pointer"]]

    if not ptr_members:
        return findings

    # Check if tp_new uses a zeroing allocator (tp_alloc, calloc, GC_New
    # followed by memset). These zero all fields including pointers.
    zeroing_patterns = [
        r"tp_alloc\s*\(",  # tp_alloc zeros memory
        r"PyType_GenericAlloc",  # zeros memory
        r"calloc\s*\(",  # zeros memory
        r"memset\s*\([^,]+,\s*0",  # explicit zero
    ]

    uses_zeroing_alloc = any(re.search(p, body) for p in zeroing_patterns)
    if uses_zeroing_alloc:
        return findings

    # tp_new uses a non-zeroing allocator (malloc, PyObject_New, etc.)
    # Check if each pointer member is explicitly initialized.
    uninitialized = []
    for m in ptr_members:
        init_patterns = [
            rf"self\s*->\s*{re.escape(m['name'])}\s*=",
            rf"->\s*{re.escape(m['name'])}\s*=\s*NULL",
            rf"->\s*{re.escape(m['name'])}\s*=\s*0",
            rf"->\s*{re.escape(m['name'])}\s*=\s*Py_None",
        ]
        initialized = any(re.search(p, body) for p in init_patterns)
        if not initialized:
            uninitialized.append(m["name"])

    if not uninitialized:
        return findings

    findings.append(
        {
            "type": "new_missing_member_init",
            "file": "",
            "function": new_name,
            "line": new_func["start_line"],
            "confidence": "medium",
            "detail": (
                f"tp_new '{new_name}' does not use a zeroing allocator and "
                f"does not initialize pointer member(s) {', '.join(uninitialized)}. "
                f"If __new__() is called without __init__(), methods may "
                f"dereference uninitialized pointers."
            ),
            "type_name": type_info["name"],
        }
    )

    return findings


def _check_new_and_init_partial_state(type_info: dict):
    """Flag types that define both tp_new and tp_init.

    Types with both have a partial-initialization window between __new__
    returning and __init__ completing. This is a triage signal — not a bug
    by itself, but it means the type is susceptible to __new__-without-__init__
    issues and should be reviewed for the more specific init_not_reinit_safe
    and new_missing_member_init patterns.

    Types with only tp_new are safe (all init is atomic). Types with only
    tp_init (inherited tp_new from base) start zeroed by tp_alloc.
    """
    findings = []
    new_name = type_info.get("new_func")
    init_name = type_info.get("init_func")

    if not new_name or not init_name:
        return findings

    # Strip casts.
    new_name_clean = re.sub(r"\([^)]*\)\s*", "", new_name).strip()
    init_name_clean = re.sub(r"\([^)]*\)\s*", "", init_name).strip()

    # Skip if tp_new is the generic default (not a custom implementation).
    generic_new_names = {
        "PyType_GenericNew",
        "PyBaseObject_Type.tp_new",
        "object_new",
    }
    if new_name_clean in generic_new_names:
        return findings

    findings.append(
        {
            "type": "new_and_init_partial_state",
            "file": "",
            "function": f"{new_name_clean} / {init_name_clean}",
            "line": type_info.get("line", 0),
            "confidence": "low",
            "detail": (
                f"Type '{type_info['name']}' defines both tp_new "
                f"('{new_name_clean}') and tp_init ('{init_name_clean}'). "
                f"This creates a partial-initialization window between "
                f"__new__ and __init__ — methods called before __init__ "
                f"(or on objects where __init__ was never called) may "
                f"encounter missing state. Review for init_not_reinit_safe "
                f"and new_missing_member_init issues."
            ),
            "type_name": type_info["name"],
        }
    )

    return findings


def _check_gc_flag(type_info: dict):
    """Check if type has traverse but no GC flag."""
    findings = []

    has_traverse = type_info.get("traverse_func") is not None
    has_gc = type_info.get("has_gc", False)

    if has_traverse and not has_gc:
        findings.append(
            {
                "type": "missing_gc_flag",
                "file": "",
                "function": "(type definition)",
                "line": type_info.get("line", 0),
                "confidence": "medium",
                "detail": (
                    f"Type '{type_info['name']}' has tp_traverse but "
                    f"doesn't have Py_TPFLAGS_HAVE_GC"
                ),
                "type_name": type_info["name"],
            }
        )

    return findings


def _check_type_spec_sentinel(tree, source_bytes: bytes):
    """Check PyType_Slot arrays end with {0, NULL}."""
    findings = []

    slot_arrays = extract_struct_initializers(tree, source_bytes, "PyType_Slot")
    for arr in slot_arrays:
        if not arr["is_array"]:
            continue
        init = arr["initializer_text"].strip()
        # Check if the last entry is {0, NULL} or {0, 0}.
        if not re.search(r"\{\s*0\s*,\s*(?:NULL|0)\s*\}\s*\}?\s*$", init):
            findings.append(
                {
                    "type": "type_spec_missing_sentinel",
                    "file": "",
                    "function": "(type definition)",
                    "line": arr["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"PyType_Slot array '{arr['variable_name']}' "
                        f"may not end with {{0, NULL}} sentinel"
                    ),
                    "array_name": arr["variable_name"],
                }
            )

    return findings


def _extract_type_infos(tree, source_bytes: bytes, functions: list[dict]) -> list[dict]:
    """Extract type definition information from PyTypeObject and PyType_Spec."""
    type_infos = []
    source_text = source_bytes.decode("utf-8", errors="replace")

    # Static PyTypeObject.
    type_inits = extract_struct_initializers(tree, source_bytes, "PyTypeObject")
    for ti in type_inits:
        fields = _parse_type_fields(ti["initializer_text"])
        flags_text = fields.get("tp_flags", "")

        info = {
            "name": ti["variable_name"],
            "line": ti["start_line"],
            "dealloc_func": fields.get("tp_dealloc"),
            "traverse_func": fields.get("tp_traverse"),
            "richcompare_func": fields.get("tp_richcompare"),
            "init_func": fields.get("tp_init"),
            "new_func": fields.get("tp_new"),
            "struct_name": _get_struct_name_from_type(fields, source_text),
            "has_gc": "Py_TPFLAGS_HAVE_GC" in flags_text,
            "is_heap_type": "Py_TPFLAGS_HEAPTYPE" in flags_text,
            "is_static": True,
        }
        type_infos.append(info)

    # PyType_Spec with slot arrays.
    spec_inits = extract_struct_initializers(tree, source_bytes, "PyType_Spec")
    for si in spec_inits:
        fields = _parse_type_fields(si["initializer_text"])
        flags_text = fields.get("flags", "")

        # Find the slots array name.
        slots_name = fields.get("slots", "").strip()
        if slots_name:
            # Look up the actual slots.
            slot_arrays = extract_struct_initializers(tree, source_bytes, "PyType_Slot")
            dealloc = traverse = richcompare = init = new = None
            for sa in slot_arrays:
                if sa["variable_name"] == slots_name:
                    slots = _parse_type_slots_array(sa["initializer_text"])
                    for slot_type, slot_func in slots:
                        if slot_type == "Py_tp_dealloc":
                            dealloc = slot_func
                        elif slot_type == "Py_tp_traverse":
                            traverse = slot_func
                        elif slot_type == "Py_tp_richcompare":
                            richcompare = slot_func
                        elif slot_type == "Py_tp_init":
                            init = slot_func
                        elif slot_type == "Py_tp_new":
                            new = slot_func

            info = {
                "name": si["variable_name"],
                "line": si["start_line"],
                "dealloc_func": dealloc,
                "traverse_func": traverse,
                "richcompare_func": richcompare,
                "init_func": init,
                "new_func": new,
                "struct_name": _get_struct_name_from_type(fields, source_text),
                "has_gc": "Py_TPFLAGS_HAVE_GC" in flags_text,
                "is_heap_type": True,  # PyType_Spec always creates heap types.
                "is_static": False,
            }
            type_infos.append(info)

    return type_infos


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for type definition correctness issues."""
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

        total_functions += len(functions)

        type_infos = _extract_type_infos(tree, source_bytes, functions)

        for ti in type_infos:
            for checker_result in [
                _check_dealloc(
                    ti,
                    functions,
                    source_bytes,
                    source_bytes.decode("utf-8", errors="replace"),
                ),
                _check_dealloc_completeness(ti, functions, tree, source_bytes),
                _check_traverse(ti, functions, tree, source_bytes),
                _check_richcompare(ti, functions, source_bytes),
                _check_gc_flag(ti),
                _check_init_reinit_safety(ti, functions, tree, source_bytes),
                _check_new_without_init(ti, functions, tree, source_bytes),
                _check_new_and_init_partial_state(ti),
            ]:
                for f in checker_result:
                    f["file"] = rel
                    findings.append(f)

        # File-level checks.
        for f in _check_type_spec_sentinel(tree, source_bytes):
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
