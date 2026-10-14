# Cortex — bounded workflows with completion evidence

Cortex turns a request into a validated plan, selects a suitable worker, and shows
what happened at each step. It combines deterministic Python tools with optional
AI models through Ollama, OpenAI and Anthropic adapters. Classification, planning,
drafting and repair share the same model-call and synthetic work budgets.

The supplier worker parses quotes, calculates feasibility in Python and verifies
the recommendation. Draft workflows check declared word limits, section headings
and currency constraints, with bounded repair. Accepted content can become a
Markdown or text file whose bytes are read back and hashed. SQLite jobs retain
accepted steps and consumed budgets across compatible resumes. Reminders record
scheduling separately from console delivery. Owner Git operations require an
explicit repository, branch and file selection, with remote commit verification.

The browser exposes plans, routing reasons, check failures, refinement history and
completion evidence. Public mode isolates visitor data and disables Git and cloud
providers. Its default enables supplier execution and reminder scheduling; AI
workflows need a privately reachable permitted backend.

Routing uses transparent rules, not a trained optimal selector. Draft checks do
not establish general factual correctness. Synthetic weights are not tokens or
money; unknown prices remain unknown. No cost savings, universal reliability or
research novelty is claimed.

## Evidence status — 13 October 2026

| Capability | Implementation and evidence | Public hosted status |
|---|---|---|
| Supplier analysis | Real Python execution; Beta/11500, four synthetic units, zero calls; installed-release check passed | Ready for a configured host; not deployed |
| AI roles and routing | Common adapters; prior live Ollama classifier/planner/worker path; OpenAI/Anthropic response checks mocked | AI disabled by default; backend required |
| Draft checks and repair | Enforced constraints, at most two repair attempts; regression and local UI evidence | Requires permitted backend |
| Document creation/download | Real file/readback/hash and protected download; prior live local evidence and installed UI evidence | Drafting unavailable with offline profile |
| Durable jobs | SQLite state, accepted-step reuse, cumulative budgets, source/config bindings | Dedicated persistent volume required |
| Reminders | Real server-console notification and receipt; installed cold service/scheduler restart check passed | Separate supervised scheduler required; no phone/browser alarm |
| Git/GitHub | Owner integration; disposable local remote verified; no live GitHub write | Disabled |
| Deployment | Wheel built reproducibly and installed/started locally | No public URL; host/TLS/storage setup missing |
| Container | Dockerfile/Compose recipes supplied | Build/start unverified: Docker/Podman absent |

No submission event requirements were supplied. This description and the assets
do not certify event compliance. Recorded examples are labelled separately from
interactive execution. All previous evidence and frozen comparison paths remain preserved.
