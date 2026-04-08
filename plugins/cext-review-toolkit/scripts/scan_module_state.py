#!/usr/bin/env python3
"""Analyze module initialization pattern and state management.

Detects single-phase init, global PyObject* state, static mutable state,
missing module traverse/clear, static type objects, and PyModule_AddObject misuse.

Usage:
    python scan_module_state.py [path] [--max-files N]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    parse_bytes_for_file,
    extract_functions,
    find_calls_in_scope,
    extract_struct_initializers,
    extract_static_declarations,
)
from scan_common import find_project_root, discover_c_files, parse_common_args


def _check_init_style(functions, source_bytes):
    """Detect single-phase vs multi-phase initialization."""
    findings = []

    for func in functions:
        if not func["name"].startswith("PyInit_"):
            continue

        body_text = func["body"]
        has_module_create = "PyModule_Create" in body_text
        has_moduledef_init = "PyModuleDef_Init" in body_text

        if has_module_create and not has_moduledef_init:
            findings.append(
                {
                    "type": "single_phase_init",
                    "file": "",
                    "function": func["name"],
                    "line": func["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"{func['name']}() uses single-phase init "
                        f"(PyModule_Create). Consider multi-phase init "
                        f"(PyModuleDef_Init + Py_mod_exec) for "
                        f"subinterpreter support"
                    ),
                    "init_style": "single_phase",
                }
            )

    return findings


def _check_global_state(tree, source_bytes):
    """Check for global mutable PyObject* and other static mutable state."""
    findings = []

    statics = extract_static_declarations(tree, source_bytes)

    for s in statics:
        if s["is_pyobject"] and not s["is_const"]:
            findings.append(
                {
                    "type": "global_pyobject_state",
                    "file": "",
                    "function": "(file scope)",
                    "line": s["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"Global mutable state: static PyObject* "
                        f"'{s['name']}' — should be in module state "
                        f"for subinterpreter support"
                    ),
                    "variable": s["name"],
                    "variable_type": s["type"],
                }
            )
        elif not s["is_const"] and not s["is_pyobject"]:
            # Non-PyObject static mutable -- flag as lower concern.
            # Skip array types (struct initializers, method tables, etc.)
            if s["type"] and any(
                t in s["type"]
                for t in (
                    "PyMethodDef",
                    "PyModuleDef",
                    "PyMemberDef",
                    "PyGetSetDef",
                    "PyType_Slot",
                    "PyModuleDef_Slot",
                )
            ):
                continue
            findings.append(
                {
                    "type": "static_mutable_state",
                    "file": "",
                    "function": "(file scope)",
                    "line": s["start_line"],
                    "confidence": "low",
                    "detail": (
                        f"Static mutable variable '{s['name']}' "
                        f"({s['type']}) — may break with "
                        f"subinterpreters if modified after init"
                    ),
                    "variable": s["name"],
                    "variable_type": s["type"],
                }
            )

    return findings


def _check_module_traverse(tree, source_bytes):
    """Check for PyModuleDef with m_size > 0 but missing m_traverse."""
    findings = []

    mod_defs = extract_struct_initializers(tree, source_bytes, "PyModuleDef")
    for md in mod_defs:
        init_text = md["initializer_text"]
        # Parse the PyModuleDef fields.
        # Order: m_base, m_name, m_doc, m_size, m_methods, m_slots,
        #        m_traverse, m_clear, m_free
        # Strip outer braces and split by commas (approximate).
        inner = init_text.strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.endswith("}"):
            inner = inner[:-1]

        # Split on commas, handling nested braces.
        fields = []
        depth = 0
        current = ""
        for ch in inner:
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
            elif ch == "," and depth == 0:
                fields.append(current.strip())
                current = ""
                continue
            current += ch
        if current.strip():
            fields.append(current.strip())

        # Field indices: 0=base, 1=name, 2=doc, 3=m_size, 4=methods,
        #                5=slots, 6=traverse, 7=clear, 8=free
        if len(fields) < 4:
            continue

        m_size = fields[3].strip()
        # Check if m_size > 0 (not 0 and not -1).
        if m_size in ("0", "-1", "0,"):
            continue

        # Check for m_traverse.
        m_traverse = fields[6].strip() if len(fields) > 6 else "NULL"
        m_clear = fields[7].strip() if len(fields) > 7 else "NULL"

        if m_traverse in ("NULL", "0", "") or m_clear in ("NULL", "0", ""):
            missing = []
            if m_traverse in ("NULL", "0", ""):
                missing.append("m_traverse")
            if m_clear in ("NULL", "0", ""):
                missing.append("m_clear")
            findings.append(
                {
                    "type": "missing_module_traverse",
                    "file": "",
                    "function": "(module definition)",
                    "line": md["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"PyModuleDef '{md['variable_name']}' has "
                        f"m_size={m_size} but missing "
                        f"{', '.join(missing)}"
                    ),
                    "module_def": md["variable_name"],
                    "m_size": m_size,
                    "missing_methods": missing,
                }
            )

    return findings


def _check_static_type_objects(tree, source_bytes):
    """Check for static PyTypeObject declarations."""
    findings = []

    type_inits = extract_struct_initializers(tree, source_bytes, "PyTypeObject")
    for ti in type_inits:
        findings.append(
            {
                "type": "static_type_object",
                "file": "",
                "function": "(file scope)",
                "line": ti["start_line"],
                "confidence": "medium",
                "detail": (
                    f"Static PyTypeObject '{ti['variable_name']}' — "
                    f"should be a heap type (PyType_FromSpec) for "
                    f"multi-phase init and subinterpreter support"
                ),
                "type_name": ti["variable_name"],
            }
        )

    return findings


def _check_module_add_object(functions, source_bytes):
    """Check for PyModule_AddObject misuse."""
    findings = []

    for func in functions:
        body = func["body_node"]
        calls = find_calls_in_scope(
            body, source_bytes, api_names={"PyModule_AddObject"}
        )
        for call in calls:
            # Check if the return value is tested.
            parent = call["node"].parent
            checked = False
            node = parent
            while node and node != body:
                if node.type in ("if_statement", "binary_expression"):
                    checked = True
                    break
                if node.type == "expression_statement":
                    break
                node = node.parent

            findings.append(
                {
                    "type": "module_add_object_misuse",
                    "file": "",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "high" if not checked else "medium",
                    "detail": (
                        f"PyModule_AddObject() used at line "
                        f"{call['start_line']} — steals reference on "
                        f"success only (pre-3.10). "
                        f"{'Return value not checked. ' if not checked else ''}"
                        f"Consider PyModule_AddObjectRef() instead"
                    ),
                    "checked": checked,
                }
            )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan C files for module state management issues."""
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

        for checker_result in [
            _check_init_style(functions, source_bytes),
            _check_global_state(tree, source_bytes),
            _check_module_traverse(tree, source_bytes),
            _check_static_type_objects(tree, source_bytes),
            _check_module_add_object(functions, source_bytes),
        ]:
            for f in checker_result:
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
