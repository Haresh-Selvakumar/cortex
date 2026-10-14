# Structured planning v1 — 9 October 2026

This checkpoint extends `product_foundation_v1` with validated plans, prompt
construction and bounded sequential execution. It retains the original supplier
workflow, providers, synthetic weights, shared call limits and verification.
Planning is opt-in; the 143-test foundation comparison path remains available.
All 175 tests now pass, including the original 143 and 32 new planning checks.

## Focused concept: a dependency needs evidence

A plan says what should happen. It does not say that anything has happened.
`depends_on` requires each prerequisite to pass its declared completion check
before its output is supplied to the next worker. Failed, missing, unsupported or
budget-blocked prerequisites block dependent steps. Independent steps may still
run. They execute sequentially in this checkpoint, not concurrently.

The supplier check uses the existing source/extraction, independent arithmetic
and feasibility gate. A draft check proves only nonempty text of a bounded size;
it does not prove factual accuracy, constraint compliance, correct code or
successful external actions. This distinction is saved with every predecessor
output and propagated into the next prompt.

## Implementation

| File | Change |
|---|---|
| `cortex/planning.py` | Schema, immutable validated plan, dependency validation/topological order, deterministic single-task plan and configured common-provider planner |
| `cortex/prompts.py` | Fixed worker instructions plus bounded JSON task/source/constraint/predecessor data |
| `cortex/plan_execution.py` | Per-step routing, prerequisite gates, output checks, statuses and aggregate outcome |
| `cortex/product.py` | Opt-in orchestration, shared classifier/planner/worker ledger and reusable existing supplier/draft workers |
| `cortex/classifier.py` | Optional supplied-source/constraint context, with a separate prompt version; ordinary foundation requests keep their original prompt |
| `cortex/product_cli.py` | Planning mode, supplied plan, source file, repeated constraints and their provenance hashes |
| `tests/test_planning.py` | 32 checks; all model HTTP is explicitly mocked |
| `scripts/check_planning_checkpoint.py` | Eleven saved offline/mocked examples, regression records and preservation audit |
| `scripts/verify_product_foundation_baseline.py` | Differential check against the frozen foundation, including exact mocked model requests |
| `examples/planning_v1/` | Readable plans for supported tasks, dependencies, missing inputs, unsupported actions, invalid cycles and source drafts |

Provider adapters, registry configuration, domain logic, supplier engine and its
policies were not replaced. The configured `roles.planner` defaults to
`local-worker`, qwen2.5:7b-instruct. `--planner-model` independently selects a
registered model. Any supported provider adapter can serve this role if its
configured model declares text generation and structured output and is available.
Planner role coverage includes Ollama, OpenAI and Anthropic with mocked HTTP;
only Ollama was live-tested.

## Plan contract

Plans contain exactly `version: 1` and one to eight `steps`. Each step contains:

| Field | Meaning |
|---|---|
| `id` | Unique 1–32 character identifier, starting with a letter |
| `task_type` | Concrete analysis, coding, documents or scheduling task; mixed requests must be decomposed |
| `goal` | Bounded step goal, at most 2 KB of UTF-8 |
| `execution_kind` | `supplier`, `text_draft` or `unsupported` |
| `required_inputs` | Declared input names: user_goal, constraints, source_material, supplier_task, predecessor_outputs |
| `missing_essential_information` | Explicit missing items; `[]` means none, not `["none"]` |
| `depends_on` | Task IDs whose checked outputs are needed |
| `required_capabilities` | Known capability names consistent with task type and actual execution contract |
| `expected_output` | supplier_recommendation, text_draft or none |
| `completion_checks` | Exactly the supported check for that execution kind |

Every step requires `user_goal`. Supplier steps also require `supplier_task`.
Dependent steps require `predecessor_outputs`, and that input is rejected without
dependencies. References must exist; duplicate IDs, self-dependencies, cycles,
invalid types, extra fields and unsupported checks are rejected before execution.
Forward references are supported through a stable topological ordering.

The supplier kind accepts only analysis/supplier_analysis capabilities and
`supplier_verified`. Text drafts accept analysis/text_generation/code_generation/
document_draft and `text_present`. A no-feasible-supplier result can pass the
supplier gate as a legitimate verified outcome without recommending a supplier.

Reminders, scheduling tools, GitHub operations, file creation, web access and code
execution are unsupported. A valid plan can declare them with `unsupported`,
expected output `none`, and check `unsupported`; the step then stops with
`checks_passed: false`. Claiming those capabilities in an executable supplier or
draft step invalidates the plan. Model tool-support declarations never grant
actual tools. A `completed` claim or invented completion check is not accepted.

Generated plans must cover the classifier's required capabilities and cannot add
unclassified action families. General analysis/text generation may support a
requested task. The foundation's bounded supplier-intent/attachment rule may bind
a broad analysis classification to the supplier worker. These are inspectable
rules, not proof that the plan fully captures the user's intent. User-authored
plans declare their own requirements and bypass AI classification/planning.

## Prompt construction and execution limits

Worker system instructions are fixed code. User goal, step goal, constraints,
selected source data, typed supplier attachment and checked direct predecessor
outputs are separately JSON-serialized as task data. Source and predecessor
content never become system instructions. Only declared direct predecessors are
included; unrelated task outputs are excluded. Source material is included when
declared relevant in `required_inputs`. Prompt separation is a defense and an
inspection boundary, not proof that a model will ignore instruction-like data.

The supplier worker's typed quantity, purchase budget and deadline remain the
authoritative Python constraints. Natural-language constraints do not override
them. Supplier model extraction retains its own field/source prompt and gate;
the trace stores both its actual model prompt and the Python execution contract.
Draft prompts are recorded as actually sent to the provider, including version.

Bounds: user goal 8 KB, additional source 12 KB, up to eight constraints of 1 KB,
plan JSON 24 KB, worker prompt 60 KB, and accepted draft output 16 KB. Missing
required source/constraints stops before invocation. Oversized prompt context
stops without silent truncation. Draft output failing its size/shape gate cannot
be consumed downstream. There is no automatic context summarizer or refinement.

Classifier, planner and workers use the same `Budget` and canonical top-level
`model_usage`. An actual provider attempt consumes a call, including HTTP failure.
Planner work has synthetic weight 1; AI classification and drafts retain weight
1; supplier actions retain their old weights. Rules and validation have weight 0.
Pre-call call/work exhaustion does not invoke a provider. Provider failure causes
explicit failure, with no automatic retry or local fallback. Existing bounded
execution-timeout and arithmetic repairs remain inside the supplier engine.

Each step records status, prompt, routing eligibility/reason, nested execution,
checked output and completion evidence. The validated plan, planner prompt,
proposed plan where bounded, diagnostic category and execution order are saved.
Top-level outcomes retain successful partial outputs when later steps fail;
dependent steps are `blocked_dependency`. `drafted` never means external work or
semantic verification completed. Do not add nested ledgers to top-level usage.

## Runnable examples

Run inside `PATH_TO_YOUR_CHECKOUT\cortex-v1`. Choose fresh output
filenames; existing results are never overwritten. Default `--planning off`
preserves the foundation behavior. `auto` uses a deterministic one-step plan for
a concrete classification and invokes the planner for decomposition. `always`
invokes the planner after successful classification. A supplied plan skips both
classifier and planner calls but still validates and routes each step.

Supported single task, entirely offline:

```powershell
python -m cortex request "Compare suppliers" --task examples/supplier_task.json --planning auto --offline --max-model-calls 0 --output runs/my_plan_single.json
```

Expected: completed, Beta 11500, four synthetic units, zero calls/tokens.

Configured live planner followed by the Python supplier worker:

```powershell
python -m cortex request "Compare suppliers" --task examples/supplier_task.json --planning always --probe-providers --max-model-calls 1 --output runs/my_planner_supplier.json
```

The normal request is rule-classified, so this permits one planner attempt. If
that plan is rejected, no supplier step runs. Live success is not guaranteed.

Independent draft tasks, with an explicit inspectable plan:

```powershell
python -m cortex request "Draft an introduction and a limitation" --plan-file examples/planning_v1/independent_drafts.json --probe-providers --max-model-calls 2 --output runs/my_plan_independent.json
```

Verified supplier result followed by a dependent draft:

```powershell
python -m cortex request "Compare suppliers and draft a short recommendation" --task examples/supplier_task.json --plan-file examples/planning_v1/supplier_then_draft.json --constraint "Under 30 words" --max-model-calls 1 --output runs/my_plan_dependency.json
```

Source material stays in task data:

```powershell
python -m cortex request "Draft from supplied source" --plan-file examples/planning_v1/source_draft.json --source examples/planning_v1/source.txt --constraint "Under 30 words" --max-model-calls 1 --output runs/my_plan_source.json
```

Automatic classification → planner → workers (requires enough calls and valid
model outputs at each gate):

```powershell
python -m cortex request "Draft a Python add function and a separate paragraph explaining it" --planning auto --probe-providers --max-model-calls 4 --output runs/my_plan_generated.json
```

Missing essentials, unsupported action, exhausted budget and invalid plan:

```powershell
python -m cortex request "Draft an announcement" --plan-file examples/planning_v1/missing_essential.json --offline --output runs/my_plan_missing.json
python -m cortex request "Create a reminder" --plan-file examples/planning_v1/unsupported_reminder.json --offline --output runs/my_plan_unsupported.json
python -m cortex request "Draft two paragraphs" --plan-file examples/planning_v1/independent_drafts.json --max-model-calls 0 --output runs/my_plan_budget.json
python -m cortex request "Draft two paragraphs" --plan-file examples/planning_v1/invalid_cycle.json --offline --output runs/my_plan_invalid.json
```

Invalid planner output/provider errors are reproducible in the mocked check
runner. No production flag fabricates a successful model response. API keys stay
in process environment variables; `.env` files are not automatically loaded.
Cloud setup remains in the foundation guide. Known keys are redacted from saved
CLI artifacts, but traces intentionally contain user/source data and should be
handled according to that data's sensitivity.

## Checks and evidence

```powershell
$env:TMPDIR = "$PWD\.test_tmp"
python -m unittest discover -s tests -q
python scripts/verify_selective_baseline.py
python scripts/verify_product_foundation_baseline.py
python scripts/check_planning_checkpoint.py --output-dir runs/my_planning_checks
```

Compilation and all **175 tests passed**. Both frozen differentials pass: the
selective check covers five paired cases plus a planner case; the product
foundation check covers seven supplier cases and two mocked classification/draft
cases, including exact provider request payloads. The existing extraction audit
passed for 216 saved development runs. All **415 prior run files**, the previous
experiment-log prefix and the frozen archives remain unchanged.

`runs/structured_planning_v1_final_offline_mocked/checks.json` records commands,
outputs, source hashes and eleven examples: single task, independent tasks,
dependency, source draft, missing essentials, unsupported reminder, budget limit,
invalid cycle, successful mocked planner, invalid mocked planner and mocked
planner timeout. Fixture token counts (20 input/10 output) are artificial; mock
latencies are harness timing, not inference performance. New tests also cover
all three planner providers, shared planner/extractor budgets, GitHub/file-action
rejection, failed prerequisite handling, partial results and context limits.

Live Ollama diagnostics used qwen2.5:7b-instruct, with five actual inference
attempts across five saved executions. They are manually revised development
checks, not independent benchmark trials or automatic retries:

| Execution | Outcome | Calls | Reported input/output | Synthetic units | Elapsed ms |
|---|---|---:|---:|---:|---:|
| Planner prompt v1 | Rejected: required user-goal input absent | 1 | 864 / 97 | 1 | 17473.60 |
| Planner prompt v2 | Rejected: supplier_task absent; also proposed unsolicited draft/scheduling work | 1 | 917 / 284 | 1 | 13070.74 |
| Planner prompt v3 | Valid plan → verified Beta 11500 | 1 | 1071 / 92 | 5 | 5325.40 |
| Supplied dependency plan, worker v1 | Verified supplier → text draft; invented `$` | 1 | 346 / 20 | 5 | 1806.94 |
| Supplied dependency plan, worker v2 | Still invented `$` despite explicit constraint | 1 | 371 / 20 | 5 | 1826.02 |

The last two results are labelled `drafted`, not semantically verified. Both
currency/constraint failures remain in the evidence, with manual inspection
notes. No completion check enforces arbitrary natural-language constraints yet.
The source's task-wide currency is unspecified; no economic interpretation of
11500 as USD is warranted. A successful planner trial is a bounded observation,
not a reliability estimate. The full classifier → planner → multi-worker chain
was tested with mocks; live planner and dependency-worker checks were separate.

`runs/structured_planning_v1_live_summary.json` indexes the originals and
`live_environment.json` records Ollama 0.31.2, model digests, Python and only
credential-presence booleans. Both cloud credentials were absent; OpenAI and
Anthropic coverage remains mocked. Intermediate source archives and per-run
prompt/code/config hashes are preserved. Prompt revisions were manual development
changes, not an implemented refinement controller. Real usage, synthetic work,
latency and money remain separate; unknown pricing/money stays `null`. Loading,
queuing, warm-up and host load were not controlled. Metadata probes are not
inference calls. No savings, novelty or optimal selection claim is made.

## Preservation and remaining work

The pre-edit snapshot is `snapshots/pre_structured_planning_v1.zip`, SHA-256
`4092ac32ad5f54e591b793adbb2b1a17edb10caa761613e70cbb4cf14439993b`.
It matches the preserved foundation archive. The new final checkpoint is
`snapshots/structured_planning_v1_checkpoint.zip`, with its checksum/change list
in the adjacent manifest. The previous 143 tests are unchanged.

Execution is sequential and in memory. Saving JSON evidence does **not** provide
resumable persistence. There is no automatic semantic verification/refinement
loop, trained router, parallel executor, document file tool, reminder tool,
GitHub tool, browser product, deployment or submission packaging yet. Classifier
and planner declarations can misrepresent intent or source relevance; structural
validation and capability coverage do not prove complete understanding. Draft
quality and constraint compliance are concrete remaining limitations. Durable
execution and bounded verification/refinement are the next product milestones.
