# Demo recording

`record_demo.cjs` records a ~100 second video of the full loop in the real operator UI:
an answered question, a question the baseline harness can't finish (which queues an
evolution job), the live evolution, and a similar question answered by the activated
harness.

```sh
uv sync --frozen --extra dev
git status            # must be clean: evolution refuses to replay a dirty source tree
node scripts/demo/record_demo.cjs artifacts/seal-demo.mp4
```

Needs Node with `playwright` resolvable (`npm i -g playwright`, or set `NODE_PATH`), a
Playwright Chromium, and `ffmpeg` for the MP4. Output lands in the ignored `artifacts/`.

The recorder starts `demo_server.py`, which needs no credentials. The UI, SQLite history,
harness, evolution controller, patch screening, Git worktrees, paired selection, oracle,
and promotion are the real code paths. Three services are local stand-ins:

| Service | Stand-in |
| --- | --- |
| OpenRouter agent model | `PolicyModel`, a deterministic tool-using policy |
| OpenRouter proposer | `ReplayProposer`, which returns the repair a real proposer produced in the live run ([verified-repair.patch](../../docs/verified-repair.patch)) |
| Daytona | The test-only local process transport (same container entry point and bridge protocol, no cloud isolation) |
| Arize Phoenix | In-memory OpenTelemetry exporter with a local span store |

For a recording against live services, run `uv run seal ui` with a configured `.env`
instead and adapt the questions to your data.
