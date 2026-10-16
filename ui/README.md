# SEAL operator console

Start with `uv run seal ui` from the repo root and open http://127.0.0.1:4173.
The console uses the same trusted execution, SQLite history, Phoenix telemetry,
and Daytona runner as the CLI. Pages expose requests, measured data reads,
model/tool activity, candidate changes, evaluation results, versions, and lineage.

Seed inventory and logistics using the README commands. The public logistics
fixture returns 2 for its warehouse-3 shipment-threshold question. The UI queues
evolution jobs for supported failures only; an ordinary unsupported request does
not expand the task contract or grader.

Run browser logic tests with `npm ci && npm test`. No cloud credentials are used.
