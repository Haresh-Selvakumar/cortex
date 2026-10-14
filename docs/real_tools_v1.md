# Real tools v1 — finalized 12 October 2026

This checkpoint extends durable_refinement_v1. Its original 203 tests and both
frozen comparisons are preserved. A full 228-test pass was recorded on 9 October;
the subsequently added read-only Git preview test passed separately on resumption.
Thus 229 tests have coverage, not a newly claimed single 229-test run. Existing
model generation, reminder deliveries and Git pushes were not repeated to finalize.

## Intent needs evidence

Planning is not execution. Registered tools use typed arguments, durable intent,
separate operation accounting and an actual completion check. Version-1 plans
and baseline prompts remain available. Opt into isolated classifier/planner
prompts with `--request-contract isolated`. They omit unrelated supplier inputs
and distinguish optional preferences from genuinely missing essentials. Validation
still rejects invalid IDs, requirements, dependencies and executable claims.

`--tools-config config/tools.example.json` enables durable, isolated tool execution.
Version-2 plans add `tool` to every step: null for workers, `{name, arguments}` for
tools. Tool steps use `tool_result` and `tool_verified`. Actual definitions are
in `cortex/tools.py`; a model's tools flag never creates an integration.

| Tool | Completion evidence | Recovery |
|---|---|---|
| write_document | New Markdown/text file, absolute path, byte size, SHA-256, exact readback | Reconcile the owned intent path; no overwrites or repair of partial files |
| reminder_create | Persistent unique reminder ID and resolved timezone/UTC trigger | Reuse the operation ID; delivery remains a separate state |
| git_inspect | Actual status, branch, HEAD, remote observation; optional intended-file diff/preview | Read-only reinspection |
| git_commit_push | Exact intended commit observed on the explicit remote branch | Inspect the operation-tagged commit and remote before repeating an ambiguous push |

Each logical tool intent reserves one synthetic work unit and consumes one of
`max_operations`. Tools use zero model calls. Reconciliation counts are recorded
separately. Model generation/refinement retains the same cumulative budget.
Scheduler delivery attempts have their own reminder ledger, rather than being
misrepresented as model calls or free model tokens. No general financial saving
is established; unknown pricing remains unknown.

File creation requires accepted predecessor text, safe `.md`/`.txt` basenames and
a configured output directory. Existing files, traversal and reserved names are
rejected. Content is read back before acceptance. This proves file content exists;
it does not prove factual correctness or event compliance. Supply authoritative
`event_requirements` in tool configuration when available; these enter draft data.
Missing requirements and unchecked compliance remain visible, never invented.

Reminders require a complete date, explicit 24-hour time and timezone. Ambiguous
AM/PM, nonexistent times, conflicting offsets and repeated DST times need explicit
resolution. UTC and numeric offsets work without dependencies. IANA zones require
installed timezone data; this Windows Python installation did not have it.
Creation means **scheduled**, not delivered. The foreground scheduler prints an
actual console notification and flushes a JSONL receipt before recording delivered.
Failed or interrupted delivery is explicit; unknown delivery is not automatically
resent. This is not an OS toast, email, mobile push or phone alarm. The scheduler
must remain running; otherwise due reminders wait until the next run.

Git requires an explicitly configured normal checkout, existing checked-out
branch and HTTPS GitHub URL or absolute disposable local remote. Push additionally
requires `allow_push: true`; default targets are empty. Inspect the intended files
first. Only named files are staged; existing unrelated staging blocks execution.
Credential patterns, known environment secrets, private runtime paths, database
files and ignored files are excluded. These conservative checks cannot establish
that arbitrary source contains no secret; review the visible intended changes.
Hooks and signing prompts are disabled for bounded execution. Branch deletion,
force pushing and history rewriting are unsupported. Failed commits can leave the
intended files staged; inspect that state. Ambiguous pushes are reconciled before
an explicitly authorized repeat, bounded to two attempts. A zero exit code alone
is insufficient: remote commit equality is required. No development-repository
push or live GitHub write was performed.

## Commands

Run inside `PATH_TO_YOUR_CHECKOUT\cortex-v1`. Use fresh output names.

```powershell
# Complete AI role path using configured local Ollama models.
python -m cortex request "Draft a Python add(a,b) function returning their sum" --request-contract isolated --classifier ai --planning always --durable --max-model-calls 4 --max-units 8 --output runs/my_live_chain.json

# Create a supplied plan with a fresh safe file name, then draft/check/write it.
python scripts/make_tool_plan.py document --filename my-project.md --output runs/my_document_plan.json
python -m cortex request "Describe Cortex from the supplied source" --tools-config config/tools.example.json --plan-file runs/my_document_plan.json --source examples/real_tools_v1/source.txt --draft-checks examples/real_tools_v1/draft_checks.json --max-model-calls 2 --max-units 3 --output runs/my_document.json

# Explicit near-future reminder; no classifier/model needed for this supplied plan.
python scripts/make_tool_plan.py reminder --after-seconds 30 --timezone UTC --message "Review my project description" --output runs/my_reminder_plan.json
python -m cortex request "Schedule the supplied reminder" --tools-config config/tools.example.json --plan-file runs/my_reminder_plan.json --offline --max-model-calls 0 --output runs/my_reminder.json
python -m cortex reminder inspect
python -m cortex reminder run --seconds 40
python -m cortex reminder cancel REMINDER_ID

# Inspect/resume an owned durable job; original limits and consumption remain.
python -m cortex job inspect JOB_ID
python -m cortex job resume JOB_ID --output runs/my_resume.json
```

For an actual dated reminder, use `--at 2026-10-13T19:00:00 --timezone +05:30`
instead of `--after-seconds`. Choose a future date when running this command.
After delivery, inspect rather than cancel: cancellation cannot undo a notification.
For a custom database/log, pass matching `--db` and `--notification-log` to the
scheduler. Job resume requires the exact bound code/input/configuration; use the
corresponding frozen source for older jobs. No migration or exactly-once guarantee
is provided.

Git setup is deliberately explicit. Copy the tool configuration and add an
`authorized` target with `path`, `branch`, `remote_url`, and `allow_push` fields.
The example plans select `description.md` and `submission-demo`; edit both plan
and authority to your expressly authorized target. Use `allow_push: false` for
inspection. Do not identify the Cortex development folder as a push target merely
to demonstrate the integration.

```powershell
python -m cortex request "Inspect my selected repository and intended files" --tools-config config/my_authority.json --plan-file examples/real_tools_v1/git_inspect.json --offline --max-model-calls 0 --output runs/my_git_preview.json
# Only after explicit authorization of the target/branch/files and allow_push:true:
python -m cortex request "Commit and push only the supplied files" --tools-config config/my_authority.json --plan-file examples/real_tools_v1/git_commit_push.json --offline --max-model-calls 0 --output runs/my_git_push.json
```

`scripts/check_real_tools_checkpoint.py` creates NEW disposable repositories and
NEW reminders when invoked. It is a demonstration/check runner, not a read-only
evidence viewer. Do not rerun it merely to view the already recorded results.

## Recorded evidence

| Evidence | What actually ran |
|---|---|
| `runs/real_tools_v1_final_live_chain.json` | Live qwen2.5:3b-instruct classifier → qwen2.5:7b-instruct planner/worker, successful checked coding draft; three calls, three synthetic units, 1488/259 reported tokens |
| `runs/real_tools_v1_live_document.json` | Live Ollama draft and real Markdown readback; one call, two synthetic units, 342/81 reported tokens, 440 bytes |
| `runs/real_tools_v1_final_checks/mixed_execution.json` | Mocked draft HTTP, real file creation, scheduled then delivered console reminder, actual repository inspection and verified push to a disposable local remote; four tools, five synthetic units |
| `runs/real_tools_v1_final_checks/notifications.jsonl` | Actual local console-delivery receipt; not an exported calendar or merely a stored reminder |
| `runs/real_tools_v1_final_checks/checks.json` | Full 228-test pass, compilation, both frozen comparisons and 216-run extraction audit |
| `runs/real_tools_v1_live_summary.json` | All nine live inference attempts across development runs, including retained invalid-plan failure; 4392/768 reported tokens total |
| `runs/real_tools_v1_initial_check_failures.json` | Transcribed environmental failure summary: sandbox denied Git MSYS helper; unwritable default temp directory; corrected execution context |

The mixed workflow used artificial 10/5 token telemetry and zero real inference
calls. Unrelated notes stayed unstaged. Terminal resume retained results/budget
without executing tools again. Original failures and intermediate runs remain
unchanged. One intermediate live execution overlapped source edits; the separately
archived stable final live path is the reproducible success evidence. Later changes
only strengthened Git paths and added read-only previews. Source/prompt/model hashes
and model digests are saved. The live document claimed 36 words; Python counted
67 and enforced the 80-word limit, without endorsing that factual self-description.

Remaining limitations: foreground console delivery, optional IANA data, no phone
alarm, no code execution, no general factual/event verifier, no job-source migration,
and no live OpenAI/Anthropic/GitHub validation. No public hosting is established by
this checkpoint. Frozen archive and changed-file manifest are in `snapshots/`.
