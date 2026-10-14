# Durable execution and bounded draft refinement v1 — 9 October 2026

This opt-in checkpoint extends `structured_planning_v1`. All 203 tests pass:
the original 175 are unchanged, with 28 new checks. Both frozen selective and
product-foundation comparison paths still match, including provider payloads.
The supplier engine, fixtures, extraction modes and verification are preserved.

## Focused concept: a saved answer needs an acceptance gate

A **drafted** step has received text. An **accepted** step has passed its declared
checks and can supply its output to a dependent step. Saving text does not prove
its correctness. For supplier analysis, acceptance still requires the existing
Python verification. For prose, acceptance proves only the declared text checks.

SQLite stores that distinction, so restarting Cortex can reuse an accepted
predecessor without asking a model to write it again. It also remembers spent
budget. If a process dies during a call, Cortex cannot know whether the server
finished it: the reservation stays charged and its outcome is **unknown**.

## Implementation and activation

| Files | Responsibility |
|---|---|
| `cortex/job_store.py`, `job_session.py`, `jobs.py` | SQLite transactions, OS execution lock, immutable bindings, call journal, resume and accepted-output integrity |
| `cortex/job_cli.py`, `app_cli.py`, `product_cli.py` | New opt-in request flags and inspect/resume commands |
| `cortex/draft_checks.py`, `refinement.py` | Validated constraints, check reports and bounded repair prompts/loop |
| `cortex/providers.py`, `classifier.py`, `planning.py` | Optional durable hooks and reuse of already received responses; ordinary provider behavior preserved |
| `cortex/product.py`, `plan_execution.py` | Step state, dependency gates, accepted-output reuse and shared generation/repair budget |
| `tests/test_durable_refinement.py` | 28 checks, mocked HTTP with real SQLite, subprocess interruption and OS-lock checks |
| `scripts/check_durable_checkpoint.py`, `examples/durable_v1/` | Reproducible mocked demonstrations, checks and preservation audit |

No third-party runtime dependency is added. Existing commands retain their
behavior. `--durable`, `--draft-checks`, `--refine`, `--max-repairs` or `--currency`
activate the new draft gate and enable planning when it was off. Durable/refine
mode defaults to one repair; checks alone default to zero. `--max-repairs 0`
disables repair; `1` and `2` are allowed, with a hard maximum of two.

The unchanged registry configuration keeps classifier/planner/worker roles
separate. `--classifier ai --planning always` requests real model classification
and planning; `--plan-file` uses a validated supplied plan. The default classifier
uses transparent rules for known tasks. `--offline` disables model providers.

## Storage and resume semantics

The default database is `.cortex/jobs.sqlite3` (Git-ignored). Use `--db PATH` to
choose another. Each job stores its request, configuration, hashes, classification,
validated plan, dependencies, prompts, routing, step states, accepted outputs,
verification reports, rejected drafts and repair history, cumulative work/calls,
usage records and timestamped transitions. SQLite snapshot and transition writes
are atomic transactions with `synchronous=FULL`.

| Step state | Meaning |
|---|---|
| `pending` | Validated step has not started |
| `running` | Execution has started; reservations precede the provider request |
| `drafted` | Response/history saved; checks may still be pending or failed |
| `accepted` | Declared completion gate passed; accepted output is hashed |
| `failed` | Execution, checking, refinement or budget has stopped the step |
| `blocked` | Prerequisite, required input or actual capability is unavailable |
| `interrupted` | Execution paused or an in-flight result is unknown |

The job itself is pending/running/accepted/failed/interrupted. A successful
draft-containing job still returns top-level `drafted`; its accepted steps do
not claim universal factual verification. A terminal failed job is evidence,
not an automatically retryable job. Resume returns the saved result for a
terminal job without calling a provider.

Resume verifies the saved input/configuration/limit hashes, resolved model
configuration, bound input files and all `cortex/*.py` hashes. Changed or missing
bound files, changed model configuration or code revisions reject execution;
inspection remains available. This compact version has no migration or relocation
mechanism. Resume has no flags to replace inputs, increase limits or reset usage.
Credential values are loaded from environment variables, not saved; credential
rotation does not alter model bindings. Job data and exports contain user source
and drafts, so treat them as local task data when sharing evidence.

An OS-owned nonblocking per-job lock prevents two execution processes using the
same database path from resuming one job at once. Process death releases the
lock; an old lock file alone does not imply ownership. Do not copy a database
into two independent execution locations and expect coordination between them.

Before HTTP, the work/call reservation, request hash and in-flight usage placeholder
are saved. After HTTP, the normalized response or explicit provider failure is
saved before interpretation. A received response can be reused after interruption
without another call. On resume, a still in-flight attempt becomes unknown, with
tokens unknown and its call charge retained. Default resume makes no replacement
call. `--retry-interrupted` explicitly authorizes a replacement, subject to the
original remaining budget and total attempt bound; it may duplicate server work.
There is **no exactly-once guarantee**, server cancellation or provider retry loop.

Accepted steps are reused. An unfinished supplier step remains an atomic worker:
its internal deterministic actions may restart and are charged again rather than
pretending partially completed work was free. Supplier steps that were accepted
are not rerun. This version resumes sequential jobs, not concurrent workflows.

## Declared checks and refinement

Example check file:

```json
{"max_words": 30, "required_sections": ["Recommendation", "Limits"]}
```

`max_words` is an integer from 1–5000. Every maximal non-whitespace run counts
as one word, including punctuation, code fences and hyphenated text. Sections
are up to eight distinct labels, matched case-insensitively as standalone lines:
`Recommendation`, `Recommendation:` or `# Recommendation` are supported.
`**Recommendation:** Beta` is not a standalone heading and fails this check.
Section presence does not verify that its contents are meaningful.

Structural nonempty-text/16 KB checks are separate from declared constraint
checks. The currency gate always runs in checked mode. Authoritative currency
comes from the task's optional `currency` field or the user's `--currency ISO`;
conflicts are rejected. It never comes from a model response or check file.
When unspecified, no denomination is allowed: the original recorded dollar
recommendations are now rejected without changing their old evidence.

Currency detection covers every Unicode `Sc` currency symbol plus these bounded
codes/names: USD/CAD/AUD (dollar/dollars), EUR (euro/euros), GBP
(pound/pounds/sterling), INR (rupee/rupees), JPY (yen), CNY (yuan/renminbi), CHF
(franc/francs), KRW (won). Matching authoritative codes/symbols/names are allowed.
Shared symbols do not establish currency identity. This lexical check can flag
ordinary words such as weight in pounds or “won”, and cannot detect every
paraphrased denomination. It is not comprehensive semantic or financial auditing.

Natural-language constraints such as “friendly tone”, step-goal instructions and
factual interpretations are not machine-verified. Supplied `--constraint` values
remain in `unchecked_constraints`; step goals remain in the plan/prompts. Reports
explicitly say `factual_verification: not_performed` and do not use model confidence.
Supplier arithmetic and feasibility results remain authoritative; these prose
checks do not establish that a draft accurately describes those results.

A failed supported check can request one repair by default. The repair receives
the original goal/constraints, source and accepted predecessor evidence, previous
draft, declared policy and specific failures, with source kept in JSON data.
The same worker model performs the repair under the same ledger. Every draft is
rechecked. Stops include passing checks, no remaining work/calls, identical
repeated output, context bound, provider failure and the repair cap. Unknown
interrupted attempts also consume the total generation-plus-repair attempt quota.
Rejected drafts stay in history but their public step output is null; dependent
steps receive only accepted outputs. Known HTTP failures are explicit and terminal,
with no automatic retries or silent provider fallback.

## Runnable commands

Run from `PATH_TO_YOUR_CHECKOUT\cortex-v1`. Choose fresh export names.
Local draft commands require the already installed Ollama service/models.

```powershell
# Deterministic durable supplier job: Beta 11500, four synthetic units, no model call.
python -m cortex request "Compare suppliers" --task examples/supplier_task.json --durable --offline --max-model-calls 0 --output runs/my_durable_supplier.json

# Accept the introduction, then pause before the dependent summary.
python -m cortex request "Draft an introduction and summarize it" --plan-file examples/durable_v1/dependent_drafts.json --durable --db .cortex/demo.sqlite3 --pause-after-step 1 --constraint "Use a friendly tone" --max-model-calls 4 --max-units 8 --output runs/my_pause.json

# Obtain the printed job_id. Inspect does not execute work.
$cortexJobId = (Get-Content runs/my_pause.json -Raw | ConvertFrom-Json).job_id
python -m cortex job inspect $cortexJobId --db .cortex/demo.sqlite3 --output runs/my_inspection.json
python -m cortex job resume $cortexJobId --db .cortex/demo.sqlite3 --output runs/my_resume.json

# After an actual in-flight interruption, only explicitly authorized replacement:
python -m cortex job resume $cortexJobId --db .cortex/demo.sqlite3 --retry-interrupted --output runs/my_authorized_resume.json

# Tight word gate with one bounded repair (model outcome is not guaranteed).
python -m cortex request "Introduce Cortex briefly" --plan-file examples/durable_v1/single_draft.json --draft-checks examples/durable_v1/word_checks.json --refine --max-model-calls 2 --output runs/my_word_gate.json

# Currency unspecified: verified supplier predecessor, checked draft and bounded repair.
python -m cortex request "Compare suppliers and draft a recommendation" --task examples/supplier_task.json --plan-file examples/durable_v1/supplier_then_checked_draft.json --durable --draft-checks examples/durable_v1/recommendation_checks.json --max-model-calls 2 --max-units 6 --output runs/my_currency_gate.json

# One call leaves no budget for refinement if generation fails a declared check.
python -m cortex request "Introduce Cortex" --plan-file examples/durable_v1/single_draft.json --draft-checks examples/durable_v1/word_checks.json --refine --max-model-calls 1 --output runs/my_budget_gate.json

# Full live role path; uncertain/missing/invalid model output stops explicitly.
python -m cortex request "Draft a Python add(a,b) function returning their sum" --classifier ai --planning always --durable --draft-checks examples/durable_v1/introduction_checks.json --probe-providers --max-model-calls 4 --max-units 10 --output runs/my_live_path.json

# Reproduce guaranteed scenarios with clearly MOCKED HTTP and real SQLite.
python scripts/check_durable_checkpoint.py --output-dir runs/my_durable_mocked_checks
```

The pause command deliberately returns exit code 2 with an interrupted result.
Ctrl+C also preserves a durable job; hard process death may prevent printing its
ID, so keep the creation export or inspect the local database. The subprocess
test exercises hard exit during the second call after an accepted predecessor.
The mocked runner demonstrates successful word/currency repair, budget-blocked
repair, failed refinement blocking a dependent, repetition stopping, accepted
predecessor reuse and unknown reservation retention. Its persuasive-tone
constraint remains unchecked. No network inference occurs in those demonstrations.

## Recorded evidence and limitations

`runs/durable_refinement_v1_offline_mocked/checks.json` records compilation,
203 tests, both exact frozen comparisons and the 216-run extraction evidence
audit, all passing. Real subprocess checks prove lock exclusion, process-death
release and durable unknown accounting with mocked provider HTTP. Seven saved
mocked scenarios use invented telemetry of 10 input/5 output tokens per response;
those are not measured model tokens. All 448 previous run files and the previous
experiment-log prefix are unchanged.

The separate offline CLI creation/inspect/resume exports are
`runs/durable_refinement_v1_offline_supplier*.json`: Beta 11500, four synthetic
units, zero calls/tokens. Terminal resume reused the result with identical counters.

Six live development executions are indexed in
`runs/durable_refinement_v1_live_summary.json`, with hashes, configurations,
latency, roles, checks and cumulative usage. Environment/model digests are in
`runs/durable_refinement_v1_live_environment.json` (Ollama 0.31.2).
Classifier: qwen2.5:3b-instruct; planner/worker: qwen2.5:7b-instruct.

| Live diagnostic | Outcome | Actual calls | Input/output tokens | Synthetic work units |
|---|---|---:|---:|---:|
| Initial full-path request | Ollama inventory unavailable; stopped | 0 | 0/0 | 0 |
| Initial supplied-plan refinement | Supplier verified; draft unavailable | 0 | 0/0 | 4 |
| Full-path request with source | Invalid mixed classification rejected | 1 | 513/70 | 1 |
| Supplier plus generation/repair | Supplier verified; repair still failed section checks | 2 | 1034/58 | 6 |
| Coding request with constraint | Classifier unnecessarily asked for supplier fields | 1 | 486/66 | 1 |
| Coding request without added context | Valid classification/plan; absent required constraints blocked worker | 2 | 1412/144 | 2 |

The installed Ollama executable started its local service during inspection;
the initial unavailable runs remain saved. Subsequent executions are separately
named manual diagnostics, not hidden retries. The live repair removed the
invented currency and reduced 29 words to seven, but inline bold headings still
failed the standalone section requirement. The supplier's Beta/11500 result
remains accepted; the rejected prose was withheld. **A complete live
classifier → planner → worker success was attempted but not achieved.** Existing
full-path mocked coverage does not establish live model reliability.

Six actual inference attempts reported 3445 input and 338 output tokens in total.
Latency is recorded per execution/provider; probes, loading and warming make
these uncontrolled diagnostics, not comparable performance trials. Synthetic
work weights are distinct from tokens, latency and money. Unknown prices remain
unknown. These rules are a deterministic recovery/refinement baseline, with no
trained selector, calibrated confidence, novelty, reliability or savings claim.
OpenAI and Anthropic credentials were absent; their integrations retain mocked
coverage and are unvalidated live.

Freeze: `snapshots/durable_refinement_v1_checkpoint.zip` and its adjacent manifest.
The pre-edit source/evidence snapshot and all earlier frozen archives are retained.
Remaining product work includes actual document-file, reminder and GitHub tools,
followed by browser/deployment/submission assets. Those actions are still unsupported;
this checkpoint stops here.
