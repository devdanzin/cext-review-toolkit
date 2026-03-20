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
│   ├── module-state-checker.md
│   ├── type-slot-checker.md
│   ├── stable-abi-checker.md
│   ├── version-compat-scanner.md
│   ├── git-history-analyzer.md
│   └── c-complexity-analyzer.md
├── commands/
│   ├── explore.md
│   ├── health.md
│   ├── hotspots.md
│   └── migrate.md
├── scripts/
│   ├── tree_sitter_utils.py
│   ├── discover_extension.py
│   ├── scan_refcounts.py
│   ├── scan_error_paths.py
│   ├── scan_null_checks.py
│   ├── scan_gil_usage.py
│   ├── scan_module_state.py
│   ├── scan_type_slots.py
│   ├── measure_c_complexity.py
│   └── analyze_history.py
└── data/
    ├── api_tables.json
    ├── deprecated_apis.json
    ├── stable_abi.json
    └── limited_api_headers.json
```

## Agents: 10

4 safety-critical (refcount, error path, null safety, GIL), 2 extension-specific (module state, type slots), 2 compatibility (stable ABI, version compat), 1 complexity, 1 history.

## Scripts: 10

8 Tree-sitter-powered analysis scripts + 1 extension discovery script + 1 shared parsing module. All output JSON to stdout.

## Data: 4 JSON files

API reference tables, deprecated API list, stable ABI function list, limited API header list.
