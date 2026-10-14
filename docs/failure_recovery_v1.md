## Failure-aware recovery checkpoint — October 8, 2026

This is a deterministic recovery baseline, not a learned policy or a novelty
claim. The mocked comparison finished at 00:36 IST and the live comparison at
00:40 IST (Asia/Calcutta); JSON evidence records UTC timestamps on October 7.

### Focused concept: identify why an operation failed

| Failure kind | What is known | Recovery rule |
| --- | --- | --- |
| Transient execution | Extraction raised `TimeoutError`; input completeness is not disproved | Retry that extraction operation at most once |
| Parsing problem | Fast format/schema handling is insufficient; aliases have not been checked | Use the existing flexible CSV/alias parser once |
| Missing information | Required fields remain absent after alias handling | Request clarification without inventing values |

`freight=500` is a supported shipping alias. It is recoverable, even though the
fast parser reports missing shipping. A quote with neither shipping nor freight
still needs clarification after flexible parsing. A timeout is an execution
failure and does not establish missing information.

### Files and behavior

Inspected the saved source/tests before edits. The original 32 tests and
compilation passed. Preserved source, tests, examples, runs and documentation in
`snapshots/pre_failure_recovery_v1.zip`. The selective-v1 checkpoint ZIP remains
unchanged. The pre-recovery ZIP and selective-v1 ZIP both have SHA-256
`ba7f723af2b76ebdf9496b0501f52eb2c77dfe98de027fe992947e95168167bf`.
There were 26 existing run JSON files, all preserved byte-for-byte. The original
experiment log is preserved as an unchanged byte prefix; this entry is appended.

- `domain.py` adds `ParsingError(DataError)` for the fast parser's unsupported
  representation. `MissingFieldsError.aliases_checked` identifies the parsing
  stage. Validation and flexible parser behavior remain intact.
- `engine.py` adds a separately selected `failure-aware` recovery branch.
  `retry_extract` repeats the failed strict/flexible extraction setting once
  per run; the allowance is shared across extraction stages. The new persistent
  timeout fault ends with `retry_limit` after one retry. An unaffordable retry
  ends with `budget_exhausted` before executing it.
- Parsing problems use bounded `repair_extract`. Missing fields after aliases
  route to clarification. Arithmetic verification still precedes recommendation;
  one recalculation is followed by another verification. Repeated arithmetic
  failure ends with `recovery_limit`. Source-fidelity mismatches retain bounded
  flexible extraction repair.
- Unsupported/unclassified cases retain planning, at most one planner action
  in the new mode. An exhausted parser repair cannot be repeated through the
  planner. Planner/controller provider failures consume an attempt and stop
  explicitly. No provider HTTP retry or local fallback was added.
- Failure classification uses exception types, stage flags and verification
  error codes. It never reads exception-message text to choose a category.
- Trace fields record `decision_failure_category` (cause known before the
  action), `failure_category` (observed failure, or the cause being handled),
  `decision_source` (`rule`/`model`), `planner_decision_source`, and retry number.
  A successful recovery event can retain its incoming cause; subsequent normal
  actions have no active failure category.
- CLI adds `--recovery-mode baseline|failure-aware`, defaulting to `baseline`.
  The new mode is supported with local policy or selective Ollama. Existing
  output-overwrite protection and provenance remain. Code checkpoint is now
  `failure-recovery-v1`; package version remains `0.1.0`.

`policies.py` and its request logic/system prompt were unchanged. The baseline
keeps its original candidate lists and model inputs; new failure fields only
enter model state when the new mode is enabled. Trace annotations and the code
checkpoint identifier are additive. The old source is available in the frozen
ZIP, and a differential check verifies baseline behavior and exact request
payloads against it.

Example new-mode command (choose a fresh output filename):

```powershell
python -m cortex examples/supplier_task.json --policy ollama --model qwen2.5:0.5b --selective --recovery-mode failure-aware --fault extract_timeout --max-model-calls 0 --output runs/my_failure_aware_timeout_v1.json
```

### Paired evaluation setup and provenance

`scripts/compare_recovery.py` compares both modes on identical normal, timeout,
arithmetic, missing-shipping and shipping-alias inputs. Every paired run has
work cap 20 and shared model-attempt cap 4. Execution order is the listed case
order, baseline then failure-aware for each case, serially. One execution per
configuration; order is not randomized. No separate planner model was selected.

Inputs: `supplier_task.json` for the first three cases,
`missing_shipping_control.json`, and `shipping_alias_control.json`.
Both modes retain the same deterministic workers and exact-decimal verifier.

Live model: `qwen2.5:0.5b`, 494.03M, Q4_K_M, installed digest
`a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`.
Python 3.13.7; Ollama server 0.31.2; localhost port 11434. Prompt version
`completion-first-v2`; temperature 0, `num_predict=256`, `think=false`, stream
disabled, JSON-schema response, 30-second request timeout. Stronger models were
not required for these supported rules and were not evaluated.

Each raw result records input/source hashes, code checkpoint, configuration,
model/prompt names, tokens, usage attempts and trace. Each comparison directory
also records runner SHA-256, execution kind and environment. The live environment
contains the full installed model inventory/digests and server version.

Evidence directories:

- `runs/failure_recovery_v1_mocked/`: ten raw runs, environment and comparison.
- `runs/failure_recovery_v1_live/`: ten raw runs, environment and comparison.
- `runs/failure_recovery_v1_persistent_timeout.json`: explicit retry-limit failure.
- `runs/failure_recovery_v1_retry_budget_blocked.json`: explicit budget-limited stop.

The mocked HTTP adapter selects the first admissible option and supplies
artificial counts of 10 prompt / 2 output per request. These counts are marked
`artificial_mock_counts`; they are not real model tokens. The configured model
name is a label in those mocked runs, not evidence of model execution. Both
modes resolve all five mocked cases. Baseline totals: 26 synthetic work units,
3 mocked attempts, artificial counts 30 / 6. New-mode totals: 25 synthetic work
units, zero requests and zero model tokens.

### Live observations

Tokens below are provider-reported prompt / output counts. Work units are
synthetic action weights. Clarification is correct only for the missing-data
fixture; the timeout and alias fixtures have complete recoverable data.

| Case | Selective baseline outcome | Baseline units | Baseline calls | Baseline tokens | Failure-aware outcome | New units | New calls/tokens |
| --- | --- | ---: | ---: | --- | --- | ---: | --- |
| Normal | Beta, 11500 | 4 | 0 | 0 / 0 | Beta, 11500 | 4 | 0 / 0 |
| One-time extraction timeout | Unnecessary clarification | 4 | 2 | 609 / 71 | Beta, 11500 | 5 | 0 / 0 |
| Arithmetic error | Beta, 11500 | 7 | 0 | 0 / 0 | Beta, 11500 | 7 | 0 / 0 |
| Missing shipping | Required clarification | 4 | 2 | 547 / 47 | Required clarification after aliases | 3 | 0 / 0 |
| Freight alias | Unnecessary clarification | 4 | 2 | 554 / 47 | Alpha, 10500 | 6 | 0 / 0 |

Totals over these five cases:

| Mode | Expected outcomes | Synthetic work units | Calls | Prompt tokens | Output tokens | Total tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Selective baseline | 3/5 | 23 | 6 | 1710 | 165 | 1875 |
| Failure-aware | 5/5 | 25 | 0 | 0 | 0 | 0 |

The new mode performs more total synthetic work here because it completes work
the baseline abandons. Baseline clarifications on timeout/alias are preserved
as failures against the expected fixture outcome. New mode performs one worker
retry, two flexible-parser repairs (one discovers missing data), one arithmetic
recalculation, and one required clarification across the five cases.

For these fixtures only, a known deterministic route exists, so model attempts
and planner actions are labeled avoidable interventions. Under that explicit
fixture oracle, baseline has 6 avoidable attempts, 3 avoidable planner actions
and 2 unnecessary clarifications. The new mode has zero of each. Necessary
repairs and the required missing-data clarification are counted separately in
the machine-readable comparisons. These categories are not added together as
a single metric because an attempt and a planner action can refer to the same
intervention.

The live baseline planner again produced unsupported explanations that remaining
units were insufficient, despite 16 remaining. Those explanations remain
unverified text. The new rules avoid needing that model decision on these
supported cases; unknown planning is still model-dependent.

Persistent-timeout boundary: `extract → retry_extract → stop`, `retry_limit`,
2 synthetic work units, zero calls/tokens. Budget boundary: one allowed unit
is spent on the failed extraction; retry is blocked, `budget_exhausted`,
zero calls/tokens. The CLI treats both as interrupted outcomes, not completion.

### Validation and limitations

Final compilation succeeded for `cortex`, `tests` and `scripts`. All **58 tests
passed**: 32 existing cases plus 26 new recovery cases. No existing behavioral
test was removed. The earlier CLI provenance assertion now follows the current
code checkpoint identifier rather than hardcoding `selective-v1`.

New cases cover the requested five behaviors, normal/CSV completion, misleading
exception messages, parser-setting replay, a per-run retry allowance shared
across stages, arithmetic/source verification, repeated arithmetic failure,
unknown planning and call-cap stopping, provider timeout without retry/fallback,
baseline model state, CLI restrictions and output preservation. A budget sweep
checks that required verification is never skipped. A local planner regression
checks that it cannot repeat an exhausted flexible repair.

`scripts/verify_selective_baseline.py` runs current and frozen selective-v1 code
in separate Python processes. On the five paired cases plus a forced planner
case, outcomes, actions, budgets, tokens and exact provider request payloads
match. This check uses mocked HTTP; it does not assert identical live generations.

- These rules explicitly cover the five supported fixtures. Observed 5/5 is
  expected deterministic mechanics, not learned generalization or reliability.
  Timeout faults are injected worker exceptions, not measured asynchronous
  execution or transport resilience. Real transient behavior can differ.
- Unknown planning is bounded but can choose poorly. Its model path was tested
  with mocks in the new mode; live calls in this paired comparison were all
  baseline calls. No stronger-model or live Jev evaluation was performed.
- Synthetic work weights do not measure full compute, tokens, money, energy or
  latency. Retrying a flexible extraction also has weight 1 while its initial
  repair has weight 2; this illustrates accounting, not measured resource cost.
  Local planner feasibility probes add parsing work within an illustrative
  planner weight. There is no monetary budget or full cost measurement yet.
- Live tokens use provider telemetry. Unknown failed-request usage stays null.
  Offline zero-request cases have zero model tokens, but still use CPU time.
- Engine latency excludes imports, input/output and experiment orchestration.
  Warm-up/cache state, hardware load and energy were uncontrolled. The sub-ms
  deterministic timings and observed model timings are not benchmark claims.
- There is one small fixture set, no repeated trials, hidden task structures,
  strong competing baseline or random ordering. No novelty, general token
  savings, monetary savings or paper-level reliability follows from this work.

Checkpoint is complete. Further scope is paused for user review.
