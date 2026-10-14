# Two-minute Cortex demo

Before recording, start the installed app and a permitted local backend. Prepare a
fresh supplier job and a fresh document job. Keep the console scheduler in another
terminal. Use the actual installed-product screenshots if live generation is slow;
say when footage is recorded. This version is a **local product demonstration**,
not a publicly deployed service. Keep owner tokens and credentials out of the recording.

| Time | Action | Spoken script |
|---|---|---|
| 0:00–0:15 | Show request input and budgets | “Cortex turns a request into a plan and an execution trace. It can use Python tools or configured AI workers. Every decision is visible, and classification, planning and repair share the same budget.” |
| 0:15–0:40 | Load supplier example, preview, execute; show route and output | “This supplier example runs the actual engine. The deterministic parser reads the quotes, Python calculates feasibility, and verification checks the result. Cortex selects Beta at 11500 using four synthetic work units and zero model calls. Synthetic weights are not tokens or monetary cost.” |
| 0:40–1:05 | Show document source, declared checks and selected worker | “For a document, I supply the facts and a word limit. Cortex chooses an eligible draft worker and keeps source material separate from execution instructions. These are declared checks, not universal factual verification. Unknown currency must stay unspecified.” |
| 1:05–1:25 | Show check report/refinement history, then accepted file result and download | “When a declared check fails, repair is bounded and consumes the same budget. Rejected content cannot reach a dependent file step. An accepted draft can be saved; Cortex reads back the bytes and returns a checksum. That proves file creation, not that every sentence is true.” |
| 1:25–1:45 | Show reminder scheduled status, console receipt and delivered status | “A reminder requires a complete time and timezone. Scheduled and delivered are separate states. The foreground scheduler produces a server-console notification and a receipt. Pending reminders survive a normal service restart when their persistent storage remains.” |
| 1:45–2:00 | Show public capability notice and close | “Public mode isolates visitors and disables owner Git and cloud access. Supplier and reminders work without AI; drafting needs a reachable permitted backend. The package is tested locally. Hosting, TLS and public visitor checks are still needed before a real tryout URL can be advertised.” |

The actual captured 40-word draft had 44 words; its one repair had 41. The dependent
save step stayed blocked. A separate 80-word request passed at 26 words and created
a verified file. These are separate jobs; the failed job's budget was not reset.
The refinement screenshot is labelled with its actual outcome. Do not present a
stopped or failed repair as a successful repair. For public offline mode, omit AI
execution and use clearly labelled recorded local evidence. Do not label localhost
as a public tryout link. No phone integration, trained routing or measured savings
is demonstrated.
