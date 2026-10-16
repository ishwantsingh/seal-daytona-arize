# Architecture

The editable harness (`harness/`) owns agent tools and context policy. The trusted supervisor (`src/self_heal/`) owns model credentials, assigned data, protected oracles, budgets, patch screening, evaluation, and promotion.

## Persistence

SQLite is the only application database. JSON documents preserve the existing domain contracts; parameterized SQL implements filters and indexed queries. Primary keys, unique indexes, and `BEGIN IMMEDIATE` protect write identities and compare-and-swap activation. Datetimes round-trip with timezone information. Dataset content hashes are reverified before use.

Tables retain immutable inventory/logistics datasets, runs, cases, candidate diffs, selection plans, trials, versions, final assessments, workflow snapshots, and evolution jobs/events. Phoenix export mappings are stored separately so evaluation publication can be retried without rerunning tasks.

## Observability and evaluation

`telemetry.py` emits OpenInference-compatible OpenTelemetry root, LLM, and tool spans to Arize Phoenix. Every root includes the local run ID; SQLite stores the resulting 32-character trace ID and UI link. The supervisor verifies that the root is retrievable before claiming complete trace evidence.

Secret fields, credential-bearing URIs, table rows, and aggregate answers are redacted before trace export. Remote traces are redacted again when read for diagnosis or UI display. Reported LLM token usage is captured at the host model boundary. Oracle correctness remains independent of trace narratives.

`phoenix_evaluations.py` publishes protected oracle scores as CODE annotations and experiment evaluations. A paired selection plan has one frozen Phoenix dataset and separate baseline/candidate experiments. Repetitions and case identities remain visible. Local export records map each evaluation to its remote experiment run, enabling safe retries after partial publication.

## Candidate execution

`runner.py` checks that source matches its clean pinned commit and that assigned data retains its immutable identity. `daytona_transport.py` stages only harness Python files and runtime shims in a fresh Daytona sandbox. Files are owned by root and made read-only; candidate execution uses `nobody`, a cleared environment, disabled outbound networking, and process/file/CPU limits. Daytona supplies the VM/container isolation boundary and snapshot resource limits.

Line-delimited JSON crosses Daytona's session stdin/stdout API with input echo disabled. The trusted supervisor services only approved model, inventory, and logistics operations, counts resource usage, and records observed tools. Candidate-provided resource counters and success claims are not authoritative. Stdout carries the protocol; candidate prints go to stderr.

Provisioning and cleanup have bounded service timeouts. Task execution has its own configured elapsed-time limit. The transport bounds individual messages and total output, rejects malformed envelopes, and deletes the sandbox on all exit paths. Cleanup failure prevents successful evidence. Automatic stop, deletion, and a fixed TTL cover abandoned clients.

Git worktrees isolate source versions. The runner identity combines the immutable Daytona snapshot ID with the trusted runtime hash; worktrees alone are not an execution boundary.

## Promotion

The supervisor reproduces a supported failure, freezes independently gradable evidence, screens a narrow model-proposed diff, and compares baseline/candidate under the same model, data, configuration, and runner identity. Promotion requires all gates and an unchanged active parent. SQLite atomically activates the exact tested child. Rollback references retained versions. Final assessment cases are reserved before selection and consumed once.
