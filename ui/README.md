# SEAL operator console

Start with `uv run seal ui` from the repo root and open http://127.0.0.1:4173.
Restart the server after pulling or merging Python API changes. Static UI files
update immediately, while the running Python process retains its loaded routes;
an old process can show the new viewer but lack its preview endpoint.
The console uses the same trusted execution, SQLite history, Phoenix telemetry,
and Daytona runner as the CLI. Pages expose requests, measured data reads,
model/tool activity, candidate changes, evaluation results, versions, and lineage.

Ask shows each question and its answer in a chat without navigating away. Each
question appears in a right-aligned bubble and replies appear as plain text.
The compact composer stays at the bottom while the conversation scrolls above
it. Enter sends, Shift+Enter adds a line, and Cmd/Ctrl+Enter also sends. Example
questions appear before the first message; run history lives on the Runs page.
The read-only database viewer stays beside the chat on the right, with table
switching for logistics sources. The Ask workspace fills the available horizontal
width with 32px side gutters. On narrow screens the viewer stacks below the chat.
Each message uses the source selected when it was sent; questions are independent
database requests, not contextual follow-ups. While a request runs, the chat
shows a pending reply and prevents duplicate submissions. Errors appear inline
and restore the submitted question unless you have already typed a new draft.
View run and View evolution links open the recorded evidence when needed.
The conversation, reply links, draft, selected source, and scroll position are
saved in this browser's local storage and restored across navigation and page
reloads. Reset chat clears the conversation, draft, and scroll position, including
saved browser state. It stays available during a request; late responses cannot
repopulate a reset chat. The selected data source is preserved, and submitted runs
remain in Runs. A request interrupted
by a reload is marked as interrupted; check Runs before sending it again.
Completed analyses also remain in Runs.

Seed inventory and logistics using the README commands. The public logistics
fixture returns 2 for its warehouse-3 shipment-threshold question. The UI queues
evolution jobs for supported failures only; an ordinary unsupported request does
not expand the task contract or grader.

Run browser logic tests with `npm ci && npm test`. No cloud credentials are used.
