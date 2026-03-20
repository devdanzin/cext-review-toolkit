---
description: "Extension modernization assessment -- multi-phase init, stable ABI, version compatibility"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Extension Migration Assessment

Run the modernization-focused agents to assess what needs to change to bring the extension up to current best practices: **module-state-checker**, **type-slot-checker**, **stable-abi-checker**, and **version-compat-scanner**. Answers the question: "What do I need to change to modernize this extension?"

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Run `python <plugin_root>/scripts/discover_extension.py [scope]` to detect the extension layout
2. If no C extension found, inform the user and stop
3. Run the four modernization agents, feeding discovery context:
   - **module-state-checker** -- init style, global state, module lifecycle
   - **type-slot-checker** -- type definition correctness and modernity
   - **stable-abi-checker** -- ABI compliance assessment
   - **version-compat-scanner** -- version compatibility and dead code
4. Synthesize into a migration report with concrete checklist:

```markdown
# Extension Migration Report

## Extension: [name]

## Current State
- Init style: [single-phase (legacy) / multi-phase]
- Global state: [N] static PyObject* variables, [N] other static mutable variables
- Type definitions: [N] types ([N] static, [N] heap)
- Stable ABI: [not used / claimed / fully compliant]
- Minimum Python: [version from python_requires or inferred]
- Dead compatibility code: [N] version guard blocks below minimum

## Migration Checklist

### Phase 1: Multi-phase Initialization
[Skip if already using multi-phase init]

**Difficulty: [Easy / Moderate / Hard]** (based on amount of global state)

- [ ] Convert `PyInit_xxx` from `PyModule_Create` to `PyModuleDef_Init`
- [ ] Add `Py_mod_exec` slot with module initialization logic
- [ ] Create module state struct to hold [N] global PyObject* variables:
  [list each variable and what it holds]
- [ ] Set `m_size` to `sizeof(module_state_struct)`
- [ ] Add `m_traverse` visiting all PyObject* in module state
- [ ] Add `m_clear` clearing all PyObject* in module state
- [ ] Add `m_free` if any non-Python cleanup is needed
- [ ] Replace direct global access with `PyModule_GetState()` calls
- [ ] Convert [N] static PyTypeObject(s) to heap types:
  [list each type]

### Phase 2: Type Slot Correctness
[Only items that need fixing -- skip if all types are correct]

For each type with issues:
- [ ] [type name]: [specific issue and fix]

### Phase 3: Stable ABI Adoption
[Skip if already compliant or if stable ABI is not desired]

**Difficulty: [Easy / Moderate / Hard]** (based on private API usage)

- [ ] Replace [N] internal struct accesses with accessor functions:
  [list each: e.g., "op->ob_type -> Py_TYPE(op)"]
- [ ] Replace [N] private API calls:
  [list each: e.g., "_PyObject_GC_TRACK -> PyObject_GC_Track"]
- [ ] Remove [N] forbidden header includes:
  [list each]
- [ ] Add `#define Py_LIMITED_API 0x03[XX]0000` before `#include <Python.h>`
- [ ] Update build system to pass `-DPy_LIMITED_API=0x03[XX]0000`

### Phase 4: Compatibility Cleanup
[Skip if no dead code or deprecated APIs]

- [ ] Remove [N] dead version guard blocks (targeting Python < [minimum]):
  [list files and line ranges]
- [ ] Replace [N] deprecated API calls:
  [list each: e.g., "PyModule_AddObject -> PyModule_AddObjectRef"]
- [ ] Consider adding `pythoncapi_compat.h` for forward-compatible macros

### Phase 5: Free-Threading Readiness (Optional)
[Only if targeting Python 3.13+]

- [ ] Add `{Py_mod_gil, Py_MOD_GIL_NOT_USED}` to module slots (or `Py_MOD_GIL_USED` if GIL is needed)
- [ ] Audit [N] static mutable variables for thread safety
- [ ] Add synchronization for shared mutable state

## Estimated Total Effort
[Summary: "N items across M phases. Core migration (Phase 1) is [difficulty]
and touches [N] files. Phases 2-5 are incremental and can be done separately."]

## References
- [PEP 489 -- Multi-phase extension module initialization](https://peps.python.org/pep-0489/)
- [PEP 384 -- Defining a Stable ABI](https://peps.python.org/pep-0384/)
- [PEP 703 -- Making the Global Interpreter Lock Optional](https://peps.python.org/pep-0703/)
- [pythoncapi-compat](https://github.com/python/pythoncapi-compat)
- [Porting C extensions to Python 3.13+](https://docs.python.org/3/howto/isolating-extensions.html)
```

## Usage

```
/cext-review-toolkit:migrate              # Full migration assessment
/cext-review-toolkit:migrate src/         # Specific directory
```
