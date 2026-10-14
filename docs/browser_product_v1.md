# Browser product v1 — local engine, durable evidence

This checkpoint extends real_tools_v1. The browser submits jobs to the existing
classifier, planner, router, supplier executor, draft checks and registered tools.
It contains no fake interactive responses. Recorded evidence is labelled separately.
Python standard library only; no frontend build or package installation required.

## Start locally (PowerShell, project directory)

```powershell
ollama serve
# In another terminal:
python -m cortex web --port 8765 --state-dir .cortex/browser
```

Open the **owner URL printed in the terminal**. Its fragment contains a temporary
bootstrap token, removed immediately from the address bar. Treat it as a local
access credential; do not publish it. Provider credentials stay in backend environment
variables, never in browser fields or traces. Use the existing `config/cortex.example.json`
to change provider endpoints, enabled models, capabilities and prices. The interface
selects registered model IDs independently for classifier, planner and worker.
Cloud adapters are available in owner mode if configured; no live cloud validation
was performed. Missing configuration/provider failures remain explicit.

The browser supports source .txt/.md attachments and supplier task .json attachments,
each bounded to 16 KB, plus editable source text. Uploaded contents are data;
paths are never submitted as filesystem authority. The JSON request limit is 64 KB;
engine prompt bounds can reject oversized combined context without truncation.

## One concept: preview is preparation, execution is a separate transition

Preview validates and persists a plan. An AI request can spend classifier/planner
calls during preview. Clicking Execute resumes that **same job**, preserving those
calls. Supplier/document/reminder/Git workflow templates declare their action family
explicitly and skip AI classification/planning. A template is not proof that it fully
captures arbitrary natural-language intent: review it. No worker/tool runs at preview.
Jobs awaiting execution use the existing `interrupted` job state, with a clear reason
and pending steps; this is a voluntary pause, not a claim of an unknown provider outcome.

The UI displays validated dependencies, per-step states, chosen worker/rule reason,
outputs, code-check failures, repair history, prompts, usage and durable transitions.
It distinguishes drafted, accepted, failed and blocked steps. Reminder schedule
acceptance is distinct from delivery. Verification covers declared checks; code
drafts are not executed, facts/event compliance remain unchecked. Currency must
come from authoritative supplier task data; unknown currency forbids denominations.
Word count is maximal non-whitespace runs, including code fences/punctuation.
Free-form constraints remain visible as unchecked. Prices/money remain unknown
when the engine cannot establish them; synthetic weights are not tokens or money.

Inspect/resume from the job list. A terminal job is reused, not repeated. Unknown
in-flight model attempts retain their consumed reservations and need the explicit
replacement checkbox. Tools reconcile their owned intent before reporting completion;
exactly-once execution is not promised. Browser jobs bind input/configuration and
Python source hashes. Changing source or the resolved registry rejects old resumes;
use the matching frozen revision for old jobs. Inspection still works.

## Files, reminders and Git

Draft → file uses validated .md/.txt basenames inside that session's output directory.
Only accepted drafts reach file creation. Readback, size and SHA-256 must pass. The
download endpoint rechecks the bytes and session ownership; changed files are rejected.
No arbitrary filesystem browser or download path exists. Existing files are not overwritten.
Supplied event requirements can be configured backend-side; missing requirements are flagged.

```powershell
# Separate terminal, same state directory; keep this process running:
python -m cortex web-reminders --state-dir .cortex/browser --seconds 3600
```

Enter a full date and 24-hour time, e.g. `2026-10-12T23:00:00+05:30`, and an explicit
timezone such as `+05:30` or `UTC`. Use a **future** date when demonstrating it. IANA
zones need installed timezone data; this Windows environment lacks it. The foreground
scheduler delivers a flushed **console message** and durable receipt; it does not
create a phone alarm or OS toast. UI state changes scheduled → delivered or delivery_failed.
Cancellation is available while scheduled. Resume does not duplicate reminders.
Closing the scheduler stops delivery until it is started again. It processes only
session databases recorded in this browser instance's store.

For owner Git actions, configure `git_targets` in a separate tools configuration;
see real_tools_v1.md. Then start with `--tools-config PATH`. The UI selects the exact
backend-authorized repository/branch, relative files and commit message. The first
Execute runs real inspection and pauses. Review its diff/content/hash, then check the
authorization box and resume. Changed head/remote/selected bytes invalidate the
inspection; tool execution rechecks the reviewed intent. Only selected safe files
are staged; exact remote commit equality is required. Ambiguous push outcomes retain
the earlier reconciliation rules. No development-repository or live GitHub push was
performed. Automated Git checks use fresh disposable local remotes.

## Session isolation and hosting

Default owner mode binds loopback only and requires its bootstrap token. HTTP-only,
SameSite=Strict cookies identify 24-hour sessions; mutation requests require exact
Origin/Host and a CSRF token. CSP prohibits injected scripts/frames and outputs use
text nodes. Jobs, reminders and output directories are separate for every session.
Session cookies identify visitor data; this is not a user-account/login system.

Public mode disables Git and cloud models, removes developer repository authority,
and requires a separate state directory and exact HTTPS reverse-proxy origin. It
allows only backend-configured Ollama models, never visitor URLs/API keys/paths.
Reserve limits are not reported usage: each new job reserves its whole allowance
from the browser session/instance pool. Resume consumes the original job allowance.
Per job: 1–40 synthetic units, 0–12 calls. One active operation per session, two per
instance. Public session allowance: 40 calls / 160 units / 20 jobs. Public instance
allowance: 200 reserved calls / 100 jobs, persistent across restarts. At most 256
sessions. These caps limit exposure; anonymous visitors can still exhaust the demo
allowance. Resetting deployment data is an operator action, not an automatic budget reset.

Public deployment is **not completed**. Use an isolated unprivileged account/container:
mount only dedicated public configuration/state; do not mount developer repositories,
home directories, `.cortex` jobs, Git credentials or cloud keys. Put Ollama on a private
network and configure `base_url` server-side; browser clients never connect to it.
The supplied public config assumes the private hostname `ollama`, not hosted localhost.
Provision the configured models and compute before serving traffic. Keep the model
endpoint inaccessible to the public internet. Persist `/data` across restarts.

`deployment/Dockerfile` and `nginx.example.conf` are **unbuilt/unvalidated deployment
recipes**. Build from the project root with `docker build -f deployment/Dockerfile .`.
Start one app process with:

```text
python -m cortex web --public --host 0.0.0.0 --port 8765 --origin https://YOUR_DOMAIN --config config/cortex.public.example.json --state-dir /data/browser
python -m cortex web-reminders --state-dir /data/browser --seconds 86400
```

The second is a separate foreground process with the same dedicated volume. Supervise
and restart it at the duration limit; retain its console log for the notification channel.
Use TLS certificates, preserve the exact external Host to the backend, limit body/rate
and idle connections at the proxy, and expose only the proxy. The standard-library
HTTP server is a compact demo server; [Python's documentation](https://docs.python.org/3.13/library/http.server.html)
does not recommend it for production. It is not a horizontally scalable cloud service.
No public DNS/TLS/container deployment, load test or live GitHub/cloud validation is claimed.

## Concrete demo sequence

1. Start local server/Ollama; open the printed owner URL.
2. Supplier mode: attach `examples/supplier_task.json`, set zero model calls; preview
   and execute. Show Beta / 11500, four synthetic units, zero calls and verification.
3. AI request: “Draft a Python function add(a, b) that returns their sum. Return a short
   coding draft.” Choose AI classification, Always use planner, 120 maximum words.
   Preview (two calls), inspect dependencies, Execute (worker call), show accepted checks.
4. Document mode: supply known Cortex facts, 40 words, fresh `.md` filename. Preview,
   execute, review checks/readback/hash and download the actual file.
5. Reminder mode: choose UTC/full future trigger and zero calls. Execute; show scheduled,
   then scheduler console notification and delivered receipt. Avoid old trigger templates.
6. Use another draft with a restrictive word cap to demonstrate a repair or bounded
   rejection; do not promise a live model will always violate or repair it. Tests provide
   clearly mocked repeatable evidence. Rejected drafts block dependent file creation.
7. Optional disposable owner Git target: inspect selected files, review, authorize,
   compare remote commit. This does not authorize a real GitHub repository write.

## Validation and evidence

`runs/browser_product_v1_freeze_checks`: compilation, **249 tests**, and both exact frozen
comparisons pass. Twenty new browser checks use actual HTTP/SQLite/file/Git/reminder
operations with explicitly mocked model responses. They cover auth/CSRF/ownership,
public restrictions, durable preview reuse, budgets, check failures, repair history,
download integrity, scheduler delivery, cancellation isolation, Git review and changed
intent rejection. No existing recorded action was re-executed.

Live browser runs and retained failures are exported to `runs/browser_product_v1_live_final/`.
The live inference source revision is also archived separately in
`runs/browser_product_v1_live_inference/source.zip`; the final revision subsequently
closed the scheduler's SQLite scan connection explicitly. Old jobs remain inspectable,
but their strict Python source binding requires their matching revision to resume.
Use `scripts/check_browser_checkpoint.py --browser-state ... --output-dir FRESH_DIR`
to export evidence without re-executing jobs. It saves raw job traces, hashes, model
identities, provider-reported tokens/latencies, generated files and receipts. The
first browser classifier attempt encountered a stopped Ollama service, consuming
one attempted call with unknown usage; it is retained. Subsequent success is separate.
An initial document template omitted source from its prompt: it saved an unrelated
urban-garden draft that passed structural checks. That failure is retained, the template
now explicitly declares source input, and a regression check asserts source separation
and presence in the actual prompt. Readback alone cannot establish semantic success.

Recorded browser outcomes: live coding classifier/planner/worker used three calls,
1774/179 reported input/output tokens and three synthetic units. The corrected document
used one call, 345/25 tokens and two synthetic units (generation + file tool). Its 19-word
draft used the supplied Cortex facts; 131-byte readback/download SHA-256 was
`ead73626966c8ba3d4e544940ce52aa954555090cfbf841e4f02476bd120aef0`.
Supplier attachment produced Beta/11500, four units, zero calls/tokens. A fresh browser
reminder used one unit and zero calls; reminder `7652047a3478e850c949977baaab5f1e`
was actually delivered by the console scheduler with one delivery attempt. Raw receipts
and current reminder state are exported separately from historical scheduling evidence.
All six browser jobs together reserved six provider attempts: five returned responses,
one connection failure has unknown token usage. Known reported tokens total 2429/234;
full-run usage and money remain unknown. These are bounded development observations.
Screenshot `runs/browser_product_v1_browser.jpg` shows accepted browser job/budget state.
The in-app browser's full-page screenshot API was unavailable; its native viewport
screenshot worked. Attachment and download flows were exercised through the browser.
The real-tools milestone remains frozen with its original 228-test recorded run,
plus the subsequently checked 229th read-only test. Previous checkpoints/runs remain intact.
