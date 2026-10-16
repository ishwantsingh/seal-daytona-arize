# Contributing

Install with `uv sync --frozen --extra dev`. Run `uv run pytest -q` and `cd ui && npm ci && npm test` before committing. Tests require no service credentials. Real cloud execution is checked separately with `uv run python scripts/live_smoke.py`.

Keep the trusted supervisor, protected evals, and editable harness boundaries explicit. Candidate code must not receive provider credentials, the SQLite file, private cases, or expected answers. Add meaningful tests for changed storage semantics, protocol boundaries, grading, or promotion decisions.

Commit reviewed changes before live evolution. Preserve exact source/model/configuration/runner identities and every comparative trial. Document an integration limitation honestly; a test double is not cloud execution evidence.

Secrets and runtime artifacts belong in ignored `.env`, `.seal/`, and `.self-heal/` locations. Never commit them or copy keys into generated code, fixtures, screenshots, or logs.
