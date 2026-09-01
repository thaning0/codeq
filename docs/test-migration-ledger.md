# Python validation retirement ledger

The frozen `v1.0.0-rc13` executable, not the former Python implementation
structure, is the CodeQ 2.0 compatibility oracle. This ledger accounts for
every Python test or validation program removed at cutover. A responsibility is
retained only when it protects public behavior, correctness, lifecycle safety,
or a demonstrated agent workflow regression.

The durable gates are:

- `compat/parity.rs`: 44 language-neutral command, JSON, plain-output,
  exit-code, resolution, disclosure, Python/TS semantic, dynamic-evidence, and
  Git A/M/D/R cases. Normal tests compare with the committed RC13 snapshot;
  `--oracle` and `--candidate` run any two executables.
- `compat/runtime.rs`: filesystem and abstract UDS, same-UID validation,
  version mismatch, daemon reuse/restart, restricted fallback, signals, idle
  exit, workspace bounds, eviction, and endpoint/process cleanup.
- `compat/workspace.rs`: per-worktree FTS isolation plus cache reuse and
  refresh after a working-tree mutation.
- Rust unit tests in `src/`: target parsing, symbol flattening, dynamic
  evidence, exact text, daemon namespace, IPC fallback, LSP timeout/shutdown,
  workspace single-flight, and child reaping.
- `benchmarks/readiness.rs`, `benchmarks/workflow_replay.rs`, and
  `benchmarks/readiness_gate.rs`: executable-level cold/warm Quant, RSS,
  historical mapping, representative four-command workflow, actionability,
  and hard-limit release gates.

## Retired test files

| Retired file | Durable responsibility | Disposition |
| --- | --- | --- |
| `test_cli_failures.py` | Parser errors, unsupported combinations, JSON/plain errors, and exit codes | parity fixtures plus typed Clap/boundary tests |
| `test_context_evidence_controls.py` | Section selection, lexical-vs-semantic provenance, test evidence, and bounded counts | parity context/evidence cases |
| `test_context_section_metadata.py` | exact/lower-bound totals and truncation metadata | parity disclosure cases |
| `test_contracts.py` | stable statuses, evidence classes, schema version, and rendering | parity snapshot; implementation-only Python dataclass tests retired |
| `test_core.py` | basic find/context/trace/review dispatch | parity command corpus; Python helper mocking retired |
| `test_correctness.py` | fail-closed paths/qualified symbols and non-promotion of lexical evidence | parity resolution and evidence cases |
| `test_cursor_context.py` | `PATH:LINE`, `PATH:LINE:COLUMN`, source selection, and cursor definition | parity cursor cases plus Rust target tests |
| `test_daemon_upgrade.py` | version/protocol mismatch, stale endpoint replacement, and safe restart | runtime contract |
| `test_dynamic.py` | Python/TS dynamic callback evidence and false-positive boundaries | parity dynamic cases plus Rust dynamic unit test |
| `test_file_context_disclosure.py` | explicit `--lines`, character/line limits, continuation, and no default source dump | parity file/source-window cases |
| `test_fts_find.py` | contentless in-memory FTS selection, ranking evidence, filters, refresh, and no persistence | parity find cases plus workspace contract |
| `test_gitdiff_status.py` | added/modified/deleted/renamed/untracked status handling | parity `review_statuses` scenario |
| `test_help.py` | public command/options/help surface | parity help/plain fixtures |
| `test_performance_hardening.py` | cache/single-flight bounds and readiness limits | Rust workspace tests, runtime contract, and readiness gate |
| `test_plain_paths.py` | copyable relative paths and bounded plain disclosure | parity plain-output fixtures |
| `test_review_edge_analysis.py` | semantic, lexical, dynamic, deleted/renamed, and test evidence separation | parity review and A/M/D/R fixtures |
| `test_review_worktree.py` | Git base resolution, dirty/untracked changes, filters, counts, and worktree isolation | parity review scenario and workspace contract |
| `test_runtime_state.py` | private permissions, endpoint choice, scratch/runtime cleanup, and namespace isolation | runtime contract and Rust runtime/daemon tests |
| `test_service_lifecycle.py` | concurrent cold initialization, timeout/crash cleanup, LRU/idle eviction, and retry | runtime contract and Rust LSP/workspace process tests |
| `test_textsearch.py` | delegation to `rg`, tracked/non-ignored-untracked visibility, filters, counts, and truncation | parity text cases plus Rust textsearch tests |
| `test_topology.py` | bounded import/importer/file topology and worktree filtering | parity file-context/topology fixtures; Python graph-helper internals retired |
| `test_readiness.py` | workload schema, cold/warm execution, timeout, cleanup, and threshold evaluation | Rust readiness runner and readiness gate |
| `test_historical_replay.py` | anonymized workflow mapping and executable query validation | immutable `0.5.2-workflows.json` evidence plus language-neutral `workflows.json` and Rust replay |
| `test_agent_utility_benchmark.py` | privacy redaction, output attribution boundaries, and non-causal reporting | immutable `1.0.0rc11-agent-utility.json` evidence is mechanically checked by the Rust gate; raw private-session parser tests are deliberately retired |

## Retired Python programs

| Program | Replacement or retirement decision |
| --- | --- |
| `quant_benchmark.py` | Replaced by executable-agnostic Rust readiness runner. The Python program duplicated the same committed end-to-end workload. |
| `readiness_gate.py` | Replaced by the Rust readiness gate. |
| `historical_workflow_replay.py` | Its anonymized 100-workflow result is retained as immutable observational evidence. Current executable validation is replaced by the committed language-neutral workflow and Rust replay; re-parsing private local session stores is data collection, not a shipping release gate. |
| `agent_utility_benchmark.py` | Its privacy-preserving aggregate and claim boundary are retained and mechanically gated. The raw-session parser is deliberately retired because it is an observational research collector, not runtime or repeatable repository validation. The current representative replay supplies executable-level actionability evidence. |

No coverage percentage or line-for-line Python implementation test was used as a
migration goal. Git history and `v1.0.0-rc13` retain the removed implementation
when a historical investigation is needed.
