# cext-review-toolkit (plugin)

C extension analysis plugin for Claude Code. See the [project README](../../README.md) for full documentation.

## Plugin Structure

```
cext-review-toolkit/
├── .claude-plugin/
│   └── plugin.json
├── agents/
│   ├── refcount-auditor.md
│   ├── error-path-analyzer.md
│   ├── null-safety-scanner.md
│   ├── gil-discipline-checker.md
│   ├── resource-lifecycle-checker.md
│   ├── module-state-checker.md
│   ├── type-slot-checker.md
│   ├── pyerr-clear-auditor.md
│   ├── stable-abi-checker.md
│   ├── version-compat-scanner.md
│   ├── parity-checker.md
│   ├── c-complexity-analyzer.md
│   └── git-history-analyzer.md
├── commands/
│   ├── explore.md
│   ├── health.md
│   ├── hotspots.md
│   └── migrate.md
├── scripts/
│   ├── tree_sitter_utils.py
│   ├── scan_common.py
│   ├── discover_extension.py
│   ├── scan_refcounts.py
│   ├── scan_error_paths.py
│   ├── scan_null_checks.py
│   ├── scan_gil_usage.py
│   ├── scan_module_state.py
│   ├── scan_type_slots.py
│   ├── scan_pyerr_clear.py
│   ├── scan_resource_lifecycle.py
│   ├── scan_version_compat.py
│   ├── measure_c_complexity.py
│   ├── analyze_history.py
│   └── run_external_tools.py
└── data/
    ├── api_tables.json
    ├── deprecated_apis.json
    ├── stable_abi.json
    ├── limited_api_headers.json
    └── resource_pairs.json
```

## Agents: 13

4 safety-critical (refcount, error path, null safety, GIL), 1 resource lifecycle, 3 extension-specific (module state, type slots, PyErr_Clear), 2 compatibility (stable ABI, version compat), 1 C/Python parity, 1 complexity, 1 history.

## Scripts: 15

11 Tree-sitter-powered analysis scripts + 1 extension discovery script + 1 external tools integration + 1 shared parsing module + 1 shared utilities module. All output JSON to stdout.

## Data: 5 JSON files

API reference tables, deprecated API list, stable ABI function list, limited API header list, resource allocation/free pairs.
