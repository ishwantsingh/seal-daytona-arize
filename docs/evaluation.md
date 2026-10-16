# Evaluation rules

A patch is a hypothesis about reusable behavior. The model, task contract, oracle, data, runner, and budgets remain fixed. Only Python files under `harness/` may change.

1. Record the incident with its task, dataset content hash, source identity, resources, and verified Phoenix trace.
2. Independently establish the correct answer or supported capability gap. Infrastructure failures require diagnosis rather than harness mutation.
3. Reproduce the baseline failure on the original input and an independently generated case before proposing a patch.
4. Screen the model's diff for edit scope, protected access, fixture identifiers, path escapes, and unsupported changes; pin a candidate commit.
5. Freeze a selection plan, then run baseline and candidate in fresh Daytona sandboxes on identical original, generated, regression, private-validation, and refusal cases. Repeat critical trials.
6. Grade outside candidate code. Require correctness, full required data reads, regression preservation, refusal preservation, complete traces/history/evaluation publication, and resource/cost limits.
7. Activate only the exact tested commit if its parent and environment are unchanged. Retain rejected trials as evidence.

Phoenix shows deterministic correctness scores as CODE annotations and paired experiments. SQLite retains the authoritative frozen selection plan and decision lineage. A candidate's explanation or self-reported success cannot establish correctness.

Rejected candidates return bounded tool-call feedback from failed original and generated development trials to the next proposal. Secrets, rows, and aggregate values are redacted; private validation inputs and final cases are never included. This lets a retry diagnose its actual execution failure without exposing the holdout answers.

The stress scenario intentionally exceeds the initial model loop's ability to page through a large table. `eval run` distinguishes matching that expected baseline failure from passing the task. The reviewed logistics tool is an existing capability; answering its public question alone does not demonstrate autonomous improvement.

Private validation is selection evidence, not an untouched final test. Reserve final datasets before evolution with `seal final reserve --manifest /tmp/seal-final.json`. An accepted active commit can be assessed once with `seal final assess`. Failed final cases are consumed too. Neither repeated development trials nor a single successful repair establish broad generalization.

Reproduction compares the unmet task contract, not identical error wording. A bounded agent may exhaust its calls on one replay and emit malformed JSON on another. Both are independently graded task failures. A successful baseline, a provider/runtime access failure, or a sandbox/supervisor failure cannot establish reproduction. Explicit capability gaps still require an unsupported baseline. Every candidate and regression must satisfy its original correctness and resource gates.
