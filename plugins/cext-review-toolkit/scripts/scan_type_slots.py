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
    parse_bytes, extract_functions, find_calls_in_scope,
    extract_struct_initializers, find_struct_members,
    get_node_text, walk_descendants, strip_comments,
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
    for m in re.finditer(r'\.(\w+)\s*=\s*([^,}]+)', init_text):
        fields[m.group(1)] = m.group(2).strip()
    return fields


def _parse_type_slots_array(init_text: str) -> list[tuple[str, str]]:
    """Parse PyType_Slot array entries like {Py_tp_dealloc, func}."""
    slots = []
    for m in re.finditer(r'\{\s*(\w+)\s*,\s*([^}]+)\}', init_text):
        slots.append((m.group(1).strip(), m.group(2).strip()))
    return slots


def _get_struct_name_from_type(type_fields: dict[str, str], source_text: str) -> str | None:
    """Try to extract the C struct name from tp_basicsize = sizeof(StructName)."""
    basicsize = type_fields.get("tp_basicsize", "")
    m = re.search(r'sizeof\s*\(\s*(\w+)\s*\)', basicsize)
    if m:
        return m.group(1)
    return None


def _check_dealloc(type_info: dict, functions: list[dict],
                    source_bytes: bytes, source_text: str):
    """Check tp_dealloc function for correctness."""
    findings = []
    dealloc_name = type_info.get("dealloc_func")
    if not dealloc_name:
        return findings

    # Strip casts like (destructor).
    dealloc_name = re.sub(r'\([^)]*\)\s*', '', dealloc_name).strip()
    dealloc_func = _find_func_by_name(functions, dealloc_name)
    if not dealloc_func:
        return findings

    body_text = strip_comments(dealloc_func["body"])
    has_gc = type_info.get("has_gc", False)

    # Check for tp_free call (strip comments to avoid false positives).
    has_tp_free = "tp_free" in body_text
    has_pyobject_del = "PyObject_Del" in body_text or "PyObject_Free" in body_text

    if not has_tp_free and not has_pyobject_del:
        findings.append({
            "type": "dealloc_missing_tp_free",
            "file": "",
            "function": dealloc_name,
            "line": dealloc_func["start_line"],
            "confidence": "high",
            "detail": (f"tp_dealloc '{dealloc_name}' doesn't call tp_free "
                       f"or any free function — memory leak"),
            "type_name": type_info["name"],
        })

    if has_pyobject_del and not has_tp_free:
        findings.append({
            "type": "dealloc_wrong_free",
            "file": "",
            "function": dealloc_name,
            "line": dealloc_func["start_line"],
            "confidence": "medium",
            "detail": (f"tp_dealloc '{dealloc_name}' uses PyObject_Del/Free "
                       f"instead of Py_TYPE(self)->tp_free — breaks inheritance"),
            "type_name": type_info["name"],
        })

    # Check for GC untrack (body_text already has comments stripped).
    if has_gc and "PyObject_GC_UnTrack" not in body_text:
        findings.append({
            "type": "dealloc_missing_untrack",
            "file": "",
            "function": dealloc_name,
            "line": dealloc_func["start_line"],
            "confidence": "high",
            "detail": (f"Type has Py_TPFLAGS_HAVE_GC but tp_dealloc "
                       f"'{dealloc_name}' doesn't call "
                       f"PyObject_GC_UnTrack"),
            "type_name": type_info["name"],
        })

    # Check for heap type DECREF.
    if type_info.get("is_heap_type", False):
        has_type_decref = (
            "Py_DECREF(Py_TYPE(" in body_text
            or "Py_XDECREF(Py_TYPE(" in body_text
            or ("tp = Py_TYPE(" in body_text and "Py_DECREF(tp)" in body_text)
            or ("type = Py_TYPE(" in body_text and "Py_DECREF(type)" in body_text)
        )
        if not has_type_decref:
            findings.append({
                "type": "heap_type_missing_type_decref",
                "file": "",
                "function": dealloc_name,
                "line": dealloc_func["start_line"],
                "confidence": "medium",
                "detail": (f"Heap type tp_dealloc '{dealloc_name}' doesn't "
                           f"Py_DECREF(Py_TYPE(self)) — type object leak"),
                "type_name": type_info["name"],
            })

    return findings


def _check_dealloc_completeness(type_info: dict, functions: list[dict],
                                 tree, source_bytes: bytes):
    """Check that dealloc XDECREF's all PyObject* struct members."""
    findings = []
    dealloc_name = type_info.get("dealloc_func")
    struct_name = type_info.get("struct_name")

    if not dealloc_name or not struct_name:
        return findings

    dealloc_name = re.sub(r'\([^)]*\)\s*', '', dealloc_name).strip()
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
            rf'Py_XDECREF\s*\([^)]*->\s*{re.escape(name)}\s*\)',
            rf'Py_DECREF\s*\([^)]*->\s*{re.escape(name)}\s*\)',
            rf'Py_CLEAR\s*\([^)]*->\s*{re.escape(name)}\s*\)',
            rf'Py_SETREF\s*\([^)]*->\s*{re.escape(name)}\s*,',
        ]
        cleaned = any(re.search(p, dealloc_body) for p in patterns)

        if not cleaned:
            findings.append({
                "type": "dealloc_missing_xdecref",
                "file": "",
                "function": dealloc_name,
                "line": dealloc_func["start_line"],
                "confidence": "high",
                "detail": (f"tp_dealloc '{dealloc_name}' doesn't XDECREF/CLEAR "
                           f"PyObject* member '{name}' of {struct_name} — "
                           f"reference leak per object destruction"),
                "type_name": type_info["name"],
                "missing_member": name,
            })

    return findings


def _check_traverse(type_info: dict, functions: list[dict],
                     tree, source_bytes: bytes):
    """Check tp_traverse visits all PyObject* members."""
    findings = []
    traverse_name = type_info.get("traverse_func")
    struct_name = type_info.get("struct_name")

    if not traverse_name or not struct_name:
        return findings

    traverse_name = re.sub(r'\([^)]*\)\s*', '', traverse_name).strip()
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
        pattern = rf'Py_VISIT\s*\([^)]*{re.escape(member["name"])}'
        if not re.search(pattern, traverse_body):
            findings.append({
                "type": "traverse_missing_member",
                "file": "",
                "function": traverse_name,
                "line": traverse_func["start_line"],
                "confidence": "high",
                "detail": (f"tp_traverse '{traverse_name}' doesn't visit "
                           f"PyObject* member '{member['name']}' of "
                           f"{struct_name}"),
                "type_name": type_info["name"],
                "missing_member": member["name"],
            })

    return findings


def _check_richcompare(type_info: dict, functions: list[dict],
                        source_bytes: bytes):
    """Check tp_richcompare for Py_NotImplemented handling."""
    findings = []
    rc_name = type_info.get("richcompare_func")
    if not rc_name:
        return findings

    rc_name = re.sub(r'\([^)]*\)\s*', '', rc_name).strip()
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
        if "Py_INCREF(Py_NotImplemented)" not in body and \
           "Py_NewRef(Py_NotImplemented)" not in body:
            findings.append({
                "type": "richcompare_not_incref_notimplemented",
                "file": "",
                "function": rc_name,
                "line": rc_func["start_line"],
                "confidence": "high",
                "detail": (f"tp_richcompare '{rc_name}' returns "
                           f"Py_NotImplemented without Py_INCREF — "
                           f"use Py_RETURN_NOTIMPLEMENTED"),
                "type_name": type_info["name"],
            })

    return findings


def _check_gc_flag(type_info: dict):
    """Check if type has traverse but no GC flag."""
    findings = []

    has_traverse = type_info.get("traverse_func") is not None
    has_gc = type_info.get("has_gc", False)

    if has_traverse and not has_gc:
        findings.append({
            "type": "missing_gc_flag",
            "file": "",
            "function": "(type definition)",
            "line": type_info.get("line", 0),
            "confidence": "medium",
            "detail": (f"Type '{type_info['name']}' has tp_traverse but "
                       f"doesn't have Py_TPFLAGS_HAVE_GC"),
            "type_name": type_info["name"],
        })

    return findings


def _check_type_spec_sentinel(tree, source_bytes: bytes):
    """Check PyType_Slot arrays end with {0, NULL}."""
    findings = []
    source_text = source_bytes.decode("utf-8", errors="replace")

    slot_arrays = extract_struct_initializers(tree, source_bytes, "PyType_Slot")
    for arr in slot_arrays:
        if not arr["is_array"]:
            continue
        init = arr["initializer_text"].strip()
        # Check if the last entry is {0, NULL} or {0, 0}.
        if not re.search(r'\{\s*0\s*,\s*(?:NULL|0)\s*\}\s*\}?\s*$', init):
            findings.append({
                "type": "type_spec_missing_sentinel",
                "file": "",
                "function": "(type definition)",
                "line": arr["start_line"],
                "confidence": "high",
                "detail": (f"PyType_Slot array '{arr['variable_name']}' "
                           f"may not end with {{0, NULL}} sentinel"),
                "array_name": arr["variable_name"],
            })

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
            dealloc = traverse = richcompare = None
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

            info = {
                "name": si["variable_name"],
                "line": si["start_line"],
                "dealloc_func": dealloc,
                "traverse_func": traverse,
                "richcompare_func": richcompare,
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

        tree = parse_bytes(source_bytes)
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
                _check_dealloc(ti, functions, source_bytes,
                               source_bytes.decode("utf-8", errors="replace")),
                _check_dealloc_completeness(ti, functions, tree, source_bytes),
                _check_traverse(ti, functions, tree, source_bytes),
                _check_richcompare(ti, functions, source_bytes),
                _check_gc_flag(ti),
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
