# Setup and operations

Run from the repository root. Install with `uv sync --frozen --extra dev`. `.env` is loaded automatically; already-exported environment variables take precedence. Never commit it.

| Setting | Purpose |
| --- | --- |
| `SEAL_DATABASE_PATH` | SQLite file; defaults to `.seal/seal.sqlite3` |
| `OPENROUTER_API_KEY` | Model calls by the trusted supervisor |
| `OPENROUTER_AGENT_MODEL` | Fixed tool-calling model used in paired trials |
| `OPENROUTER_EVOLUTION_MODEL` | Model that diagnoses and proposes harness changes |
| `DAYTONA_API_KEY` | Creates and manages candidate sandboxes |
| `DAYTONA_SNAPSHOT` | Runner snapshot name; defaults to `seal-python311-v1` |
| `PHOENIX_BASE_URL` | Phoenix API/UI app URL |
| `PHOENIX_COLLECTOR_ENDPOINT` | OTLP/HTTP endpoint; `/v1/traces` appended if omitted |
| `PHOENIX_API_KEY` | Phoenix Cloud authentication; optional for local Phoenix |
| `PHOENIX_PROJECT_NAME` | Trace project; defaults to `seal` |
| `PHOENIX_TRACING` | Defaults to true; disabling it blocks evidence-gated evolution |

## Phoenix

Cloud URLs must include `/s/<handle>`. Copy the app URL from your Phoenix workspace and use a key authorized for that app. Configure the collector as `APP_URL/v1/traces`. The Cloud root responds with HTTP 410 because apps are addressed by handle.

For self-hosted development, install the optional server with `uv sync --extra phoenix --extra dev`, start `uv run phoenix serve`, and set both endpoint variables to `http://localhost:6006`. Use Phoenix 13.9+; the client retrieves complete traces by trace ID. Local Phoenix needs no key unless authentication is enabled.

## Daytona

`uv run seal runner build` creates the minimal Python snapshot if missing, or reuses the existing snapshot. Selection pins its immutable ID plus a hash of all trusted runtime shim files. Baseline and candidate share that identity. The supervisor uploads only Python harness files and reviewed runtime shims; the host repository is never cloned into a sandbox.

Run `uv run python scripts/live_smoke.py` to check the real session bridge without model calls. Every trial provisions a fresh sandbox and deletes it after completion or failure. Candidate networking is blocked. Automatic stop, deletion, and TTL back up explicit cleanup.

## Local state and recovery

SQLite creates its parent directory automatically and uses WAL, indexes, unique constraints, and immediate transactions for atomic conditional writes. Back up the database with SQLite's backup API or stop the app before copying the file. Keep `.seal/`, `.self-heal/`, and `.env` private; `.self-heal/` contains pinned Git worktrees.

`seal history run --run-id ID`, `seal history lineage --run-id ID`, and `seal history active` inspect durable evidence. `seal phoenix-sync` retries unpublished evaluations without rerunning an agent. A tracing outage does not justify promotion. Re-run an incident after connectivity is restored if its original trace was never exported.

## Troubleshooting

- **Phoenix HTTP 410:** include `/s/<handle>` in both Cloud URLs.
- **Phoenix HTTP 401/403:** verify key permissions and app handle.
- **Incomplete trace:** verify collector URL, project, service reachability, and read access. Answers may still be valid; evolution requires complete evidence.
- **Missing Daytona snapshot:** run `seal runner build` with a valid API key.
- **Dirty source:** commit reviewed changes before evolution so the incident can pin a reproducible version.
- **Rejected patch:** inspect the candidate's frozen trials and gate reasons. The judge and limits remain fixed.
- **Sandbox cleanup failure:** inspect the sandbox ID in run evidence, remove it in Daytona, then verify account connectivity before another run.

The UI binds loopback by default and is intended for local operation. `--host` is an explicit exposure choice; the operator API is not a hosted multi-user authentication service.
