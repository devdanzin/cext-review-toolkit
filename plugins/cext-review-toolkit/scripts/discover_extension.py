#!/usr/bin/env python3
"""Detect C extension source files in diverse project layouts.

Tries detection methods in order (first match wins):
1. setup.py with ext_modules
2. pyproject.toml (setuptools, meson-python, scikit-build)
3. meson.build with py.extension_module()
4. CMakeLists.txt with pybind11_add_module() or Python3_add_library()
5. Fallback: scan for #include <Python.h>

Usage:
    python discover_extension.py [path]
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import EXCLUDE_DIRS


def _should_skip(path: Path, root: Path) -> bool:
    """Check if path is under an excluded directory."""
    try:
        parts = set(path.relative_to(root).parts)
    except ValueError:
        return True
    return bool(parts & EXCLUDE_DIRS)


def _find_c_files(root: Path) -> list[Path]:
    """Find all .c files under root, excluding common build dirs."""
    result = []
    if not root.is_dir():
        return result
    for p in sorted(root.rglob("*.c")):
        if not p.is_file():
            continue
        if _should_skip(p, root):
            continue
        result.append(p)
    return result


def _find_h_files(root: Path, c_files: list[Path]) -> list[Path]:
    """Find header files related to the C source files."""
    dirs = {f.parent for f in c_files}
    headers = []
    for d in dirs:
        for h in sorted(d.glob("*.h")):
            if h.is_file() and not _should_skip(h, root):
                headers.append(h)
    return headers


def _detect_setup_py(root: Path) -> list[dict] | None:
    """Detect extensions from setup.py with ext_modules."""
    setup_py = root / "setup.py"
    if not setup_py.is_file():
        return None

    try:
        content = setup_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if "Extension(" not in content and "ext_modules" not in content:
        return None

    extensions = []
    # Find Extension(...) calls.
    # Pattern: Extension("name", sources=[...])  or  Extension("name", [...])
    ext_pattern = re.compile(
        r'Extension\s*\(\s*["\']([^"\']+)["\']\s*,\s*'
        r'(?:sources\s*=\s*)?'
        r'\[([^\]]*)\]',
        re.DOTALL,
    )
    for m in ext_pattern.finditer(content):
        mod_name = m.group(1)
        sources_text = m.group(2)
        # Extract quoted filenames.
        source_files = re.findall(r'["\']([^"\']+)["\']', sources_text)
        extensions.append({
            "module_name": mod_name,
            "source_files": source_files,
            "detection_method": "setup_py",
        })

    return extensions if extensions else None


def _detect_pyproject_toml(root: Path) -> list[dict] | None:
    """Detect extensions from pyproject.toml."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None

    try:
        content = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    extensions = []

    # Check for [tool.setuptools.ext-modules]
    if "tool.setuptools.ext-modules" in content or "ext-modules" in content:
        # Parse ext-modules entries: [[tool.setuptools.ext-modules]]
        ext_pattern = re.compile(
            r'\[\[tool\.setuptools\.ext-modules\]\]\s*\n(.*?)(?=\n\[|\Z)',
            re.DOTALL,
        )
        for m in ext_pattern.finditer(content):
            block = m.group(1)
            name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
            sources_m = re.search(r'sources\s*=\s*\[([^\]]*)\]', block)
            if name_m:
                mod_name = name_m.group(1)
                source_files = []
                if sources_m:
                    source_files = re.findall(r'"([^"]+)"', sources_m.group(1))
                extensions.append({
                    "module_name": mod_name,
                    "source_files": source_files,
                    "detection_method": "pyproject_toml",
                })

    # Check for meson-python
    if "tool.meson-python" in content or "meson-python" in content:
        meson_result = _detect_meson_build(root)
        if meson_result:
            return meson_result

    # Check for scikit-build
    if "tool.scikit-build" in content or "tool.scikit-build-core" in content:
        cmake_result = _detect_cmake(root)
        if cmake_result:
            return cmake_result

    return extensions if extensions else None


def _detect_meson_build(root: Path) -> list[dict] | None:
    """Detect extensions from meson.build."""
    meson = root / "meson.build"
    if not meson.is_file():
        return None

    try:
        content = meson.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if "extension_module(" not in content:
        return None

    extensions = []
    # Match py.extension_module('name', sources: [...]) or
    # py.extension_module('name', ['file1.c', ...])
    ext_pattern = re.compile(
        r"extension_module\s*\(\s*'([^']+)'\s*,\s*"
        r"(?:sources\s*:\s*)?"
        r"\[([^\]]*)\]",
        re.DOTALL,
    )
    for m in ext_pattern.finditer(content):
        mod_name = m.group(1)
        sources_text = m.group(2)
        source_files = re.findall(r"'([^']+)'", sources_text)
        extensions.append({
            "module_name": mod_name,
            "source_files": source_files,
            "detection_method": "meson_build",
        })

    return extensions if extensions else None


def _detect_cmake(root: Path) -> list[dict] | None:
    """Detect extensions from CMakeLists.txt."""
    cmake = root / "CMakeLists.txt"
    if not cmake.is_file():
        return None

    try:
        content = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    extensions = []

    # Match pybind11_add_module(name file1.c file2.c)
    for pattern_name, method in [
        (r"pybind11_add_module", "cmake_pybind11"),
        (r"Python3_add_library", "cmake_python3"),
    ]:
        pat = re.compile(
            pattern_name + r'\s*\(\s*(\w+)\s+(.*?)\)',
            re.DOTALL,
        )
        for m in pat.finditer(content):
            mod_name = m.group(1)
            sources_text = m.group(2)
            # Sources are whitespace-separated, skip keywords like MODULE/SHARED.
            tokens = sources_text.split()
            source_files = [
                t for t in tokens
                if t.endswith((".c", ".cpp", ".cxx", ".cc"))
            ]
            extensions.append({
                "module_name": mod_name,
                "source_files": source_files,
                "detection_method": method,
            })

    return extensions if extensions else None


def _detect_python_h_fallback(root: Path) -> list[dict] | None:
    """Fallback: find all .c files that include Python.h."""
    c_files = _find_c_files(root)
    python_c_files = []

    for cf in c_files:
        try:
            content = cf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r'#\s*include\s*[<"]Python\.h[">]', content):
            python_c_files.append(cf)

    if not python_c_files:
        return None

    # Group by directory as a rough module boundary.
    extensions = []
    dir_groups: dict[Path, list[Path]] = {}
    for f in python_c_files:
        dir_groups.setdefault(f.parent, []).append(f)

    for directory, files in dir_groups.items():
        # Try to infer module name from PyInit_ functions.
        mod_name = directory.name
        for f in files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r'PyMODINIT_FUNC\s+PyInit_(\w+)', content)
            if m:
                mod_name = m.group(1)
                break

        rel_files = []
        for f in files:
            try:
                rel_files.append(str(f.relative_to(root)))
            except ValueError:
                rel_files.append(str(f))

        extensions.append({
            "module_name": mod_name,
            "source_files": rel_files,
            "detection_method": "python_h_include",
        })

    return extensions


def _scan_init_functions(root: Path, c_files: list[str]) -> dict[str, str]:
    """Scan C files for PyInit_* functions."""
    init_funcs = {}
    for rel_path in c_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'PyMODINIT_FUNC\s+PyInit_(\w+)', content)
        if m:
            init_funcs[rel_path] = f"PyInit_{m.group(1)}"
    return init_funcs


def _scan_limited_api(root: Path, c_files: list[str]) -> tuple[bool, str | None]:
    """Check for Py_LIMITED_API define in C files."""
    for rel_path in c_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'#\s*define\s+Py_LIMITED_API\s+(0x[0-9A-Fa-f]+|\w+)?', content)
        if m:
            version_val = m.group(1) if m.group(1) else None
            return True, version_val
    return False, None


def _get_python_requires(root: Path) -> str | None:
    """Extract python_requires from setup.py or pyproject.toml."""
    setup_py = root / "setup.py"
    if setup_py.is_file():
        try:
            content = setup_py.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', content)
            if m:
                return m.group(1)
        except OSError:
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
            if m:
                return m.group(1)
        except OSError:
            pass

    return None


def _count_lines(root: Path, c_files: list[str]) -> int:
    """Count total lines in C source files."""
    total = 0
    for rel_path in c_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            total += content.count("\n") + 1
        except OSError:
            pass
    return total


def discover(target: str) -> dict:
    """Discover C extension projects at the given path."""
    target_path = Path(target).resolve()
    if target_path.is_file():
        root = target_path.parent
    else:
        root = target_path

    # Try detection methods in order.
    extensions = None
    for detect_fn in [
        _detect_setup_py,
        _detect_pyproject_toml,
        _detect_meson_build,
        _detect_cmake,
        _detect_python_h_fallback,
    ]:
        extensions = detect_fn(root)
        if extensions:
            break

    if not extensions:
        extensions = []

    # Resolve source files and find headers.
    all_c_files = set()
    for ext in extensions:
        # Add header files.
        source_paths = []
        for sf in ext["source_files"]:
            full = root / sf
            if full.is_file():
                source_paths.append(full)
                all_c_files.add(sf)
        ext["header_files"] = [
            str(h.relative_to(root))
            for h in _find_h_files(root, source_paths)
        ]

    all_c_files_list = sorted(all_c_files)

    # Scan for init functions and limited API across all source files.
    init_functions = _scan_init_functions(root, all_c_files_list)
    limited_api, limited_api_version = _scan_limited_api(root, all_c_files_list)
    python_requires = _get_python_requires(root)
    total_lines = _count_lines(root, all_c_files_list)

    # Also count .c files found by scanning (may be more than what's listed in extensions).
    total_c_files = len(_find_c_files(root))

    return {
        "project_root": str(root),
        "scan_root": str(target_path),
        "extensions": extensions,
        "python_requires": python_requires,
        "limited_api": limited_api,
        "limited_api_version": limited_api_version,
        "init_functions": init_functions,
        "total_c_files": total_c_files,
        "total_lines": total_lines,
    }


def main() -> None:
    try:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        result = discover(target)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
