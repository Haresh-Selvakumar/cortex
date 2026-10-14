# Heterogeneous extraction checkpoint — October 8, 2026

This checkpoint introduces replaceable extraction workers and a deterministic
router. It does not train a governor, fine-tune a model, or establish novelty.

## Focused concept: a proposal must pass a gate

The governor decides **when** extraction happens. The extraction router decides
**which worker** reads the source. The model proposes fields and source excerpts;
Python checks the proposal, calculates totals, checks feasibility and chooses
the final supplier. A model's confidence is not part of this contract.

| Mode | Selection rule | Model requests |
| --- | --- | --- |
| `parser` | Existing deterministic JSON/CSV/alias parser | None |
| `llm` | Ollama extraction worker | One per permitted extraction execution |
| `parser-first` | Parser for JSON/CSV; model for supported plain text | Only the plain-text branch |

Malformed or incomplete structured data remains on the parser branch. Parser
repair can handle aliases; it cannot invent missing values. Routing is a
fixed representation rule, not a learned selector or estimated value function.

`--extractor-model` is independent of controller `--model` and
`--planner-model`. Worker, controller and planner attempts reserve the same
`Budget` immediately before HTTP. Failed requests consume an attempt. Worker
provider timeouts/HTTP failures stop as `worker_failed`; no automatic provider
retry or silent local fallback occurs. Existing synthetic extraction timeout
faults still follow failure-aware recovery's single retry. These are different
failure boundaries.

## Contract and verification

Model extraction accepts at most 12000 UTF-8 source bytes, eight quotes, eight
excerpts per field, 512 UTF-8 bytes per excerpt and a bounded response body.
Plain-text quotes require `Supplier NAME:` headings and explicit labeled facts.
The `Note:` region is excluded from quote evidence. Source text is passed as
untrusted `SOURCE_DATA`, with no instruction-execution or tool interface.

Output contains `supplier`, `unit_price`, `shipping`, `quantity_available`,
`delivery_days`, field-specific `evidence` and unresolved `issues`. Money uses
decimal strings; stock and days use integers, excluding booleans. Absent
shipping remains explicit `null`; an explicit zero can be accepted with support.
No quote with missing, conflicting or unsupported required values reaches
calculation.

The gate checks exact schema keys, types, bounds, nonnegative values, quote
coverage, duplicates, excerpt occurrence, supplier attribution and labeled
numeric support. It checks all labeled values in each quote region, so choosing
one of two conflicting amounts cannot conceal the conflict. Narrow guards reject
ranges/conditions in field clauses, explicit negation, added/excluded tax and
mixed currency symbols. Structured model proposals are independently compared
with the flexible parser. Verification rechecks the extraction artifact without
another model call, then independently recomputes arithmetic and feasibility.

These are conservative lexical checks, not general semantic verification.
Excerpt presence does not prove interpretation, source truth or authenticity.
Arbitrary paraphrases, qualifications and currency notation are outside the
validated development interface. Scope guards are incomplete language patterns;
unrecognized semantic qualifications can still evade them. A stronger plain-text
parser could cover this narrow language without an LLM; that comparison has not
been performed. The existing-parser comparison therefore does not prove a model
is necessary for these documents.

## Development evaluation

`evaluation/extraction_v1/suite.json` contains eight cases and 70 independently
handwritten expected fields, assigned before live extraction. It covers existing
JSON, CSV aliases, JSON freight, clear plain text, missing shipping in text,
conflicting shipping, instruction-like distracting text and existing JSON with
missing shipping. Five cases should complete; three should clarify.

Every comparison runs identical inputs through all three modes with a local
deterministic governor, failure-aware recovery, 20 synthetic work units and four
shared model attempts per task. Thus live requests here are extraction-worker
requests. Combined controller/planner/worker limits are also tested with mocked
HTTP. The comparison is serial in parser, LLM, parser-first order; there is no
randomization, controlled warm-up or repeated-trial reliability estimate.

The field metric scores proposals, including rejected extractions: exact
supplier/integer matches and Decimal money equivalence. A missing output/key
earns no credit, including when the expected field is null. Each extra supplier
adds five scored errors. Field accuracy is therefore distinct from accepted
extraction and final task outcome. A safe clarification can match an expected
outcome despite extracting no fields. On a complete source, clarification is
an unnecessary intervention and a task failure.

An unnecessary worker call means a call on a structured case already covered
by the existing parser, including its clarification behavior. This is an
explicit coverage-based definition, not an oracle for globally optimal cost.

`heldout_wording.json` is reserved for later evaluation. The runner never loads
it; it was not used in prompts, worker tests or these comparisons. Its recorded
SHA-256 is `0bbccb542cf661708330dc2be3b9b8440f18f0801a7c29d2fc40057ecd8c4d0a`.
This preparation is not a completed held-out or hidden generalization study.

## Provenance and revisions

Before edits, compilation and the 58 existing tests passed. The source, tests,
examples, runs and documentation were preserved in
`snapshots/pre_heterogeneous_extraction_v1.zip`, identical to the failure-aware
checkpoint: SHA-256
`1165ef1414e3cc5c10ee3a72368465eebe4a4a82f89a965e4e2dceb511b9d5cf`.
All 53 earlier run files and the earlier experiment-log byte prefix remain
unchanged. The selective-v1 archive is retained.

CLI records include actual Python module/input hashes, configuration, model
labels, prompt version and timestamps. Comparison directories include the
runner, extraction module, environment/model inventory and independently saved
task inputs. The final source is frozen in
`snapshots/heterogeneous_extraction_v1_checkpoint.zip`.

Live server: Ollama 0.31.2. Installed models used:

| Model | Quantization | Model digest |
| --- | --- | --- |
| `qwen2.5:3b-instruct` | Q4_K_M | `357c53fb659c5076de1d65ccb0b397446227b71a42be9d1603d46168015c9e4b` |
| `qwen2.5:7b-instruct` | Q4_K_M | `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e` |

Requests use schema-constrained output, temperature 0, `think: false`, at most
3072 generation tokens and a 30-second provider timeout. Actual server context,
hardware load, warm-up and enforcement are uncontrolled; installed model
metadata is not a measurement of active runtime context.

- `supplier-extraction-v1`: initial live attempts failed with HTTP 400. The
  schema's 12000-character excerpt repetition exceeded the local grammar
  compiler's bound. All failed attempts remain preserved with unknown tokens.
- `supplier-extraction-v2`: excerpts reduced to 512 bytes; HTTP error details
  retained; label/value citation instructions added. 3B outputs omitted fields
  and suppliers. 7B often got field values right but reformatted excerpts, so
  validation rejected them. A later v2 validator revision added the narrow
  tax/negation/currency guards; module hashes distinguish these versions.
- `supplier-extraction-v3`: explicit field mapping, exact-copy instructions and
  the response schema are included in the prompt as well as decoder settings.
  This follows [Ollama's schema-grounding guidance](https://docs.ollama.com/capabilities/structured-outputs).
  Development revisions are visible in the evidence; they are not independent
  test-set gains or proof that the revision caused any outcome change.

## Final live observations: prompt v3, 7B

| Mode | Expected task outcomes | Proposed fields correct | Accepted extractions | Calls | Avoidable calls under parser coverage | Prompt / output tokens | Synthetic work units | Total engine latency |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| Parser only | 6/8 | 40/70 | 3 | 0 | 0 | 0 / 0 | 37 | 0.003 s |
| LLM only | 5/8 | 52/70 | 2 | 8 | 4 | Unknown total | 14 | 98.356 s |
| Parser first | 7/8 | 67/70 | 4 | 4 | 0 | 3288 / 864 | 26 | 37.996 s |

LLM-only includes one provider timeout, whose tokens are unknown. The other
seven responses reported 5665 prompt / 1525 output tokens; these are known
subtotals, not totals for all eight attempts. Parser-first used 4152 reported
tokens in total. Model fields were accepted only after verification.

| Case | Parser only | LLM only | Parser first |
| --- | --- | --- | --- |
| Existing JSON | Beta, 11500 | Provider timeout | Beta, 11500 |
| Existing CSV/aliases | Beta, 11500 | Beta, 11500 | Beta, 11500 |
| JSON freight alias | Alpha, 10500 | Unnecessary clarification | Alpha, 10500 |
| Clear plain text | Unnecessary clarification | Beta, 11500 | Beta, 11500 |
| Missing shipping in text | Required clarification | Required clarification | Required clarification |
| Conflicting shipping in text | Required clarification | Required clarification | Required clarification |
| Instruction-like distraction | Unnecessary clarification | Unnecessary clarification | Unnecessary clarification |
| Missing shipping in JSON | Required clarification | Required clarification | Required clarification |

The clear-text result demonstrates a live model-backed path through extraction,
source checks and independent Python calculation. It does not establish general
extraction capability. On the distracting-note case the 7B model returned null
shipping for both suppliers despite explicit amounts in the quote paragraphs.
Validation stopped before calculation. This preserves a safe failure, but the
task was not solved. The single case does not establish whether the note caused
the omission. On conflicting shipping the model proposed a specific amount
rather than null; the full-region conflict check rejected that proposal.

There were no incorrect recommendations in these final observations. Only two
LLM-only extractions and four parser-first extractions reached recommendation,
so that observation is not evidence of broad reliability or injection resistance.
Parser-first avoided four requests relative to LLM-only on this suite, under
the explicitly defined parser-coverage rule. This is not a claim about monetary
savings, total execution-cost optimality, or general tasks.

## Retained development failures and mock checks

All directories below contain 24 raw results, eight task inputs, environment,
comparison and exact runner/worker copies. Directory names are immutable; the
earlier directory containing `final_live_7b` used prompt v2, while the final
prompt-v3 comparison is `heterogeneous_extraction_v3_live_7b`.

| Live directory under `runs/` | Prompt | Model | LLM outcomes | Parser-first outcomes | LLM / parser-first calls | Reported prompt/output tokens: LLM; parser-first |
| --- | --- | --- | ---: | ---: | --- | --- |
| `heterogeneous_extraction_v1_live` | v1 | 3B | 0/8 | 4/8 | 8 / 4 | Unknown; unknown (HTTP 400) |
| `heterogeneous_extraction_v2_live` | v2 | 3B | 3/8 | 6/8 | 8 / 4 | 2349/3211; 1208/1248 |
| `heterogeneous_extraction_v1_final_live_7b` | v2 | 7B | 3/8 | 6/8 | 8 / 4 | Unknown; 1208/1084 |
| `heterogeneous_extraction_v3_live_3b` | v3 | 3B | 3/8 | 6/8 | 8 / 4 | 6509/2017; 3288/1010 |
| `heterogeneous_extraction_v3_live_7b` | v3 | 7B | 5/8 | 7/8 | 8 / 4 | Unknown; 3288/864 |

The 3B v3 response to clear text omitted Beta and returned the name `Supplier
Alpha` instead of `Alpha`. Other responses omitted required values or supplied
unusable excerpts. All model extractions in the v2 comparisons and the v3 3B
comparison were rejected. The earlier v2 7B first JSON request also timed out.
Unknown-token attempts remain unknown, and a failed request was never retried
automatically. Across the five live development comparisons there were 60
actual attempts; separate runs are explicit development experiments, not API
retry behavior. Their unknown telemetry prevents an exact all-experiment token
total.

The four mock directories (`heterogeneous_extraction_v1_mocked`,
`heterogeneous_extraction_v2_mocked`, `heterogeneous_extraction_v1_final_mocked`,
`heterogeneous_extraction_v3_mocked`) supplied oracle responses to test routing,
contracts and accounting. All report parser 6/8, LLM 8/8 and parser-first 8/8.
Mock LLM/PF fields are 70/70; their attempts are 8/4; artificial prompt/output
counts are 160/32 and 80/16. These counts and scores are **not** real usage or
model-quality evidence. Every raw result identifies mocked/live execution.

## Validation and practical limits

Final compilation succeeded for `cortex`, `tests` and `scripts`. All **107 tests
passed**, including all 58 earlier tests and 49 new worker/scope tests. New
checks cover exact evidence, omitted/invalid fields, null shipping, conflicts,
unsupported business terms, instruction-like notes, parser routing, arithmetic
repair, shared controller/planner/worker caps, provider failures without retries,
bounded HTTP diagnostics, run-scoped telemetry and CLI wiring. No historical
test was removed.

The frozen selective-v1 differential check still matches outcomes, actions,
budgets, tokens and exact model request payloads on five paired cases plus a
planner case, with mocked HTTP. `scripts/verify_extraction_evidence.py` audited
216 saved raw development runs across nine comparison directories, recomputed
their field/outcome totals, checked budgets, source/runner/input hashes and
verified all 53 earlier files and the log prefix remain unchanged.

Tests needed a project-local temporary directory because this sandbox denied
the default Windows temporary path. Python also printed a launcher-path warning
while returning successful checks; the runtime version recorded in runs is
3.13.7. Local Ollama access required the host's network permission boundary.
These environment observations are separate from worker quality.

- Synthetic weights are fixed action labels, not tokens, time, CPU work, money
  or energy. A model extraction has weight 1 just like parser extraction, despite
  very different real usage. Lower weights can also reflect abandoned work.
- Timings cover engine/worker execution; imports, file I/O and experiment
  orchestration are excluded. Provider latency includes loading/queuing where
  applicable. Warm-up and machine load were not controlled. A timeout may have
  consumed unreported server work. No dollars or energy were measured.
- One small development suite and visible prompt revisions do not estimate
  generalization, reliability, calibrated uncertainty or novelty. No held-out
  run, strong plain-text parser baseline, trained router, fine-tuning or full
  cost-optimization study was performed.
- The source gate has narrow language coverage and can reject valid paraphrases
  or fail to recognize unsupported qualifications. Model confidence and exact
  excerpts cannot close that gap. The 12 KB bound is not a measured guarantee
  that every configured model's active context can fit the source and prompt.

This bounded checkpoint is complete; broader work is paused for user review.
