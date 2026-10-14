# Public tryout and submission preparation — 13 October 2026

No compatible persistent-server hosting target or deployment credentials were
configured. Docker/Podman is absent on this Windows host. Consequently **no public
deployment, public URL or container build/start validation is claimed**. The wheel
is built, installed into an isolated environment, and started outside the checkout.
Two builds using fixed settings produce the same wheel SHA-256. Runtime dependencies
are empty. Tested runtime: Windows, Python 3.13.7; Linux/container execution remains
unvalidated. The 249-test foundation and earlier evidence stay frozen.

## Install and start the verified Python release

From a directory containing the provided wheel (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --no-deps .\friday_cortex-0.2.0-py3-none-any.whl
.\.venv\Scripts\python.exe -m cortex web --state-dir .\cortex-state
```

Open the printed **local owner URL**. It is not a tryout URL; do not share its
bootstrap token. Start the scheduler in a separate terminal:

```powershell
.\.venv\Scripts\python.exe -m cortex web-reminders --state-dir .\cortex-state --seconds 86400
```

Unix equivalent: `python3 -m venv .venv`, then `.venv/bin/python -m pip ...` and
`.venv/bin/python -m cortex ...`. These Unix commands are supplied, not live-validated.
Installed defaults include UI assets, safe example configuration and a supplier
fixture. Explicit missing configuration paths fail; they are not silently replaced.
Configuration supplied with `--config`/`--tools-config` remains backend-only. Cortex
reads credential environment variables, not a `.env` file. Public mode strips cloud
providers and Git authority even if an owner configuration is accidentally supplied.

Build from source with the pinned build environment:

```powershell
python -m pip install -r requirements-build.txt
python scripts/sync_package_defaults.py
python scripts/build_release.py --output releases/my-fresh-build --source
```

The pinned tools used here were setuptools 81.0.0 and wheel 0.46.3. Fixed
SOURCE_DATE_EPOCH/PYTHONHASHSEED and a source allowlist support repeatability.
Build outputs must be new directories. The source release excludes credentials,
private runtime databases, runs and snapshots. The wheel includes only application
code, UI/package data and distribution metadata. Do not install the thumbnail's
development imaging dependency to run Cortex.

## Public startup and missing hosting setup

Provision an unprivileged persistent Linux/server environment or a container host,
then configure a TLS reverse proxy with an actual domain and trusted certificate.
Expose only the HTTPS ingress; keep the Python listener private. Public mode requires
the exact external origin. Use a separate state volume from owner mode:

```text
python -m cortex web --public --host 0.0.0.0 --port 8765 --origin https://YOUR_DOMAIN --state-dir /data/public-cortex
python -m cortex web-reminders --state-dir /data/public-cortex --seconds 86400
```

The public default is an **offline profile**: model specifications are disabled.
Supplier execution and reminder scheduling remain interactive; AI/document drafting
is explicitly unavailable. A model-inventory check reports reachability/listing,
not competence. It is bounded, cached briefly and consumes no inference reservation.
Unavailable AI requests are rejected before a job allowance is reserved. Provider
failures during actual execution remain explicit; no hidden provider retry/fallback.
All original budgets and frozen comparison paths remain intact.

To enable AI, supply a backend configuration whose Ollama URL is reachable **privately
from the application**. The original provider rule permits HTTP only on loopback;
non-loopback URLs must use HTTPS without embedded credentials. The earlier unvalidated
`http://ollama:11434` container example violated that rule. The corrected example uses
a reserved HTTPS placeholder; replace it with a real trusted private TLS endpoint.
Alternatively colocate an explicitly permitted model service in the same isolated
loopback namespace. Never publish the developer's Ollama port or owner URL, loosen
TLS validation, or expose developer accounts merely to enable the demo. Public cloud
and Git access stay disabled. Model identities/capabilities/prices remain configured
declarations; unknown pricing stays unknown.

Required setup still missing:

1. Hosting account/project or server address plus deployment access.
2. Persistent volume owned by the service account; enough capacity for jobs/files.
3. Public HTTPS origin, DNS and trusted certificate/reverse-proxy configuration.
4. Two supervised processes: web and console scheduler, same dedicated volume.
5. Optional privately reachable permitted model endpoint, installed model IDs and
   adequate compute; TLS trust material if using a private CA.
6. A container runtime/build runner if choosing the provided Docker recipes.

`deployment/compose.yaml` is an **unvalidated recipe**, not a completed deployment.
It shares a named state volume, drops capabilities, uses a read-only root, waits for
web health before scheduler startup and requests automatic restarts. It binds the
application port on host loopback; add a TLS ingress. Set CORTEX_PUBLIC_ORIGIN first.
An optional Ollama profile publishes no port and still needs an appropriate private
TLS/loopback connection arrangement. A placeholder domain is never a real tryout link.

```text
docker compose -f deployment/compose.yaml build
docker compose -f deployment/compose.yaml up -d web scheduler
docker compose -f deployment/compose.yaml logs -f scheduler
```

The current standard-library HTTP server is for a bounded tryout behind a proxy,
not a claimed hardened, scalable production service. Apply ingress request/body/time
limits, an access policy and monitoring. Public instance reservations are finite;
anonymous visitors can exhaust them. No load/security certification is claimed.

## Persistence and delivery semantics

Persist the entire state directory: browser session/quota SQLite, session job/reminder
SQLite, outputs and notification receipts. Back up consistently while services are
stopped or with SQLite backup APIs. Do not mount developer home/repositories/private
jobs or credentials. Exactly one web instance and one scheduler instance are intended.
In-memory operation polling is lost on web restart; inspect/resume the persisted job.
Accepted steps and consumed budgets are retained. Compatible code/configuration is
required to resume; earlier frozen revisions must be used for their source-bound jobs.

Installed-release checks confirmed a pending reminder survives a stopped web service,
overdue time and fresh scheduler startup, and delivers once. A normal scheduler stop
also preserves pending rows. Delivery requires a running supervised scheduler; downtime
delays notifications. Killing the scheduler after claiming an in-flight delivery may
leave an unknown outcome, explicitly marked delivery_failed on restart and not resent.
No exactly-once promise. The channel is a flushed **server-console message plus receipt**,
not a phone alarm, email, OS toast or unsolicited browser notification. A visitor sees
the receipt/status through their session; it is not push delivery to their device.
Browser cookies expire after 24 hours; session data and pending reminders are not
automatically purged. An expired cookie does not grant access to old job data.

## Visitor verification before advertising a URL

After actual deployment, use a fresh browser profile with no owner cookie. Verify:
public session bootstrap without an owner URL; no Git/cloud options; independent
second session cannot read/resume/download/cancel first-session records; actual sample
supplier yields Beta/11500 and four synthetic units/zero calls; permitted model path
creates an accepted document with exact protected-download checksum, or is visibly
unavailable; reminder scheduled then actual console receipt/delivered state after a
normal restart; Host/Origin/CSRF/TLS checks; no arbitrary config/path fields accepted.
Only then record the actual public HTTPS URL. These external checks have not occurred.

Local installed public-boundary tests passed with two fresh unauthenticated API
clients on a loopback listener simulating the private proxy boundary. They are not
public browser/TLS/deployment evidence. Supplier result, isolation, owner denial,
unavailable AI and actual console delivery/restart were checked. Original HTTP/file
download/regression evidence remains preserved; local installed UI screenshots and
live worker traces are stored separately.

## Submission assets

`submission/public_submission_v1/` contains the two-minute script, project description,
actual installed-product screenshots and the original 1200 × 800 PNG thumbnail
(under 5 MB). The thumbnail is branding, not invented execution evidence. Screenshots
are labelled as local owner execution; repair failures are shown honestly. See the
asset manifest for source/job/checksum provenance. No event requirements were supplied,
so these assets do not claim submission compliance.

Validation: all **254 tests** passed, including the original 249 unchanged tests;
both frozen comparisons match. The installed offline public-mode check used fresh
visitors and executed supplier/reminder actions with no model calls. A local installed
owner document run used two live Ollama calls, failed 40-word checks at 44/41 words,
and blocked file creation. A separate 80-word request used one call, passed at 26
words and created a 164-byte file. Its actual protected browser download matched
SHA-256 `f3e9901f7b707b1f10e342eb15d67465d592dd1859f6a40a3d9dfcb5eab2a0b4`.
Reported tokens were 816/113 for the failed generation/repair and 345/32 for the
successful generation. Costs remain unknown. Model inventory/startup problems and
the earlier unvalidated configuration defect are documented, not silently hidden.
The actual screenshot model service was started explicitly on **127.0.0.1** only;
it is not an advertised hosted backend. These local development observations do
not establish reliability or a public capability guarantee.
