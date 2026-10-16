# Live verification — October 16, 2026

SEAL completed a real failure-to-repair cycle using Daytona and Arize Phoenix Cloud. The generated repair passed selection and was activated locally. This is evidence for one inventory aggregation repair, not a claim of broad generalization.

## What ran

- Agent: `openai/gpt-6-luna` through OpenRouter.
- Proposer: `anthropic/claude-sonnet-5` through OpenRouter.
- Baseline: `7350f4f7cc833397ecb6e5cdd530c38070c99f0b`.
- Generated candidate: `8656fe5ebb7c4ceaf0e63facb7575a40fffb0423`.
- Selection plan: `plan_2bbe8dc97bdd63c9cc9b2b32ca1ba4c5`.
- Incident: `787b52eb-4258-4305-89a9-d883803739ba`.

The original 512-row grouped aggregation failed within the initial model loop. SEAL independently reproduced the baseline failure on the original input and a generated case. The proposer then added a reusable bounded aggregation tool and clarified how to preserve the validated task's filters. Only `harness/tools.py` and `harness/context.py` changed. No answers, fixture identifiers, oracle changes, or budget increases were included.

The [exact generated patch](verified-repair.patch) is saved for review. The repository's default harness remains the initial baseline so a fresh database can demonstrate the repair process with `seal demo`. The running workspace uses the accepted commit through SQLite's active-version pointer.

## Paired selection

Every trial ran in a fresh Daytona sandbox. All 28 traces and correctness evaluations were available in Phoenix before promotion.

| Case role | Baseline passes | Candidate passes |
| --- | ---: | ---: |
| Original failure, two repetitions | 0/2 | 2/2 |
| Generated reproduction | 0/1 | 1/1 |
| Existing regressions | 2/2 | 2/2 |
| Four private variations, two repetitions each | 2/8 | 8/8 |
| Unrelated refusal | 1/1 | 1/1 |
| Total | 5/14 | 14/14 |

One original candidate trial read all 128 bounded pages in one tool call, used 3 model calls and 1,868 tokens, and completed in 24.5 seconds. The supervisor independently checked the result and complete data coverage.

The post-promotion verification also passed on the 512-row case. Its [Phoenix trace](https://app.phoenix.arize.com/s/divyanshiparashar25/projects/UHJvamVjdDoz/traces/6a3bc51cbbd6677eb8a795a9cbe8da6f) contains the observed model/tool execution.

## Untouched final assessment

Two final datasets were reserved at `2026-10-16T15:47:52Z`, before the fresh incident and selection. They were excluded from candidate selection and assessed once against the accepted commit. Both passed with complete recorded history and Phoenix evaluations.

| Final case | Rows in dataset | Pages read | Model calls | Elapsed |
| --- | ---: | ---: | ---: | ---: |
| Filtered scalar aggregation | 72 | 6 | 3 | 12.1 s |
| Grouped aggregation | 512 | 128 | 3 | 17.0 s |

The scalar case reads only matching rows; the grouped case reads all rows. [Scalar trace](https://app.phoenix.arize.com/s/divyanshiparashar25/projects/UHJvamVjdDoz/traces/8f46c1de591612f15f6ba933f89ebcfc) and [grouped trace](https://app.phoenix.arize.com/s/divyanshiparashar25/projects/UHJvamVjdDoz/traces/4862a8fa7eb3caf8033d08bda009ee39) are available in the configured Phoenix app. These two passes are a small final check, not a statistical generalization estimate.

## Engineering checks

- 122 credential-free Python tests pass, including real SQLite persistence/concurrency and the isolated runtime bridge.
- 15 UI tests pass; the dependency audit reports zero vulnerabilities.
- GitHub CI passes on Python 3.11 and 3.12 and Node 22.
- Live `seal doctor` confirms SQLite, Phoenix, Daytona, and the pinned runner snapshot are ready.
- Runtime code and the dependency lock contain no legacy database or tracing dependencies.

Earlier rejected and interrupted development attempts remain in local history. A provider default that filled optional tool parameters was corrected before this fresh incident: the adapter now explicitly preserves non-strict optional tool schemas, and that setting is included in execution identity. Those earlier attempts did not activate a candidate.
