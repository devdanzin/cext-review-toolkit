"""Tests for measure_c_complexity.py — C function complexity analysis."""

import pytest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

complexity = import_script("measure_c_complexity")


def test_simple_function_low_complexity():
    """Linear function scores low."""
    code = """\
#include <Python.h>

static PyObject *
simple_func(PyObject *self, PyObject *args)
{
    PyObject *result = PyLong_FromLong(42);
    return result;
}
"""
    with TempExtension({"simple.c": code}) as root:
        result = complexity.analyze(str(root / "simple.c"))
        assert result["functions_analyzed"] == 1
        func = result["hotspots"][0]
        assert func["name"] == "simple_func"
        assert func["score"] < 3.0
        assert func["cyclomatic_complexity"] == 1
        assert func["nesting_depth"] <= 1


def test_nested_function_high_complexity():
    """Deeply nested function scores high."""
    code = """\
static int
complex_func(int a, int b, int c, int d, int e, int f, int g)
{
    int result = 0;
    if (a > 0) {
        if (b > 0) {
            if (c > 0) {
                if (d > 0) {
                    if (e > 0) {
                        if (f > 0) {
                            for (int i = 0; i < g; i++) {
                                if (i % 2 == 0) {
                                    result += i;
                                } else if (i % 3 == 0) {
                                    result -= i;
                                } else {
                                    while (result > 0 && i < 100) {
                                        result--;
                                        if (result == 50 || result == 25) {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    return result;
}
"""
    with TempExtension({"complex.c": code}) as root:
        result = complexity.analyze(str(root / "complex.c"))
        assert result["functions_analyzed"] == 1
        func = result["hotspots"][0]
        assert func["name"] == "complex_func"
        assert func["nesting_depth"] >= 6
        assert func["cyclomatic_complexity"] > 5
        assert func["score"] >= 1.0


def test_function_with_gotos():
    """Goto count is tracked."""
    code = """\
static int
goto_func(int x)
{
    if (x < 0)
        goto error;
    if (x > 100)
        goto error;
    if (x == 50)
        goto error;
    return 0;
error:
    return -1;
}
"""
    with TempExtension({"goto.c": code}) as root:
        result = complexity.analyze(str(root / "goto.c"))
        assert result["functions_analyzed"] == 1
        func = result["hotspots"][0]
        assert func["name"] == "goto_func"
        assert func["goto_count"] == 3


def test_multiple_functions_ranked():
    """Hotspots ordered by score."""
    code = """\
static int simple(void) { return 0; }

static int
medium(int a, int b)
{
    if (a > 0) {
        if (b > 0) {
            return a + b;
        }
        return a;
    }
    return 0;
}

static int
complex_one(int a, int b, int c)
{
    int r = 0;
    for (int i = 0; i < a; i++) {
        if (i % 2 == 0) {
            for (int j = 0; j < b; j++) {
                if (j % 3 == 0) {
                    while (c > 0) {
                        if (c % 2 == 0 || c % 5 == 0) {
                            r += c;
                        }
                        c--;
                    }
                }
            }
        }
    }
    return r;
}
"""
    with TempExtension({"multi.c": code}) as root:
        result = complexity.analyze(str(root / "multi.c"))
        assert result["functions_analyzed"] == 3
        # Hotspots should be ordered by score descending.
        scores = [h["score"] for h in result["hotspots"]]
        assert scores == sorted(scores, reverse=True)
        # Most complex function should be first.
        assert result["hotspots"][0]["name"] == "complex_one"


def test_empty_file_no_crash():
    """Empty or non-C file handled gracefully."""
    with TempExtension({"empty.c": ""}) as root:
        result = complexity.analyze(str(root / "empty.c"))
        assert result["functions_analyzed"] == 0
        assert len(result["hotspots"]) == 0


def test_summary_statistics():
    """Summary contains expected fields."""
    with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
        result = complexity.analyze(str(root / "myext.c"))
        summary = result["summary"]
        assert "total_functions" in summary
        assert "hotspot_count" in summary
        assert "avg_cyclomatic" in summary
        assert "avg_line_count" in summary
        assert "max_nesting" in summary
        assert summary["total_functions"] >= 2
