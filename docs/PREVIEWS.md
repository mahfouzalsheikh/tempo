# Local static previews

After a React mini-app candidate passes its required checks and retains a ZIP, its
idea page offers **Start local preview**, **Open preview**, and **Stop preview**.
The preview serves that ZIP's bytes, without checking out source, installing packages,
rebuilding, or invoking an agent. It is a testing stage, not production promotion or
proof that the brief's acceptance criteria have passed.

The first supported target is the browser on the Tempo host. Each launch uses a random
`http://<identifier>.localhost:8031/` origin; the Compose port binds only to `127.0.0.1`.
This path has been exercised in Chromium. A remote browser's localhost points to its
own machine; these links are not public or shared team deployments. `TEMPO_PREVIEW_PORT`
can change the local port in both services. Standalone Tempo installations additionally
need an absolute `TEMPO_PREVIEW_ROOT` and the isolated serving process.

## Evidence and lifecycle

Authenticated, CSRF-protected POST controls scope the artifact to its product and run.
Guarded idea pages and controls use the operator middleware's no-store policy and vary
by Cookie and Authorization, so pages containing preview access links are not cached.
Launching rechecks the ZIP and manifest digests, saved product contract, successful
candidate checkpoint, and passed validation evidence. All ZIP members must match the
saved inventory; traversal paths, links, duplicate entries, private configuration names,
oversized files, and missing entry pages are rejected. The serving process checks each
returned file's size and SHA-256, and never extracts or executes project files.

Migration `0017_preview_deployment` retains the current artifact, random link identity,
initiating operator, timestamps, expiry, and active state. An artifact row lock serializes
start, repair, renewal, and stop operations on PostgreSQL. Repeated starts reuse an intact,
unexpired preview. A stopped, expired, or missing preview gets a fresh identifier on its
next start, after rechecking the retained build. Old links remain revoked. If stored preview
bytes are damaged, stop and restart from the idea page to restore them.

The server reads an independent versioned file contract in the `tempo-previews` volume.
File contents and directory updates are synchronized before publication; authorization
metadata is removed before deletion. Ordinary process/container restarts preserve previews.
A crash between file publication and the database commit can leave an unlisted preview;
its unknown link still expires, and a subsequent start creates a new recorded link.
Missing files fail closed and can be restored from the database artifact.

Links expire after 24 hours. Every request checks the expiry, including after reading
its bytes. Expired files and abandoned staging directories older than a day are reclaimed
on the next launch; stopping deletes the preview immediately. No scheduled storage sweeper
or installation-wide quota exists yet. Revocation prevents subsequent retrieval, but cannot
recall files already downloaded or stop JavaScript in an already open browser tab.

The ZIP and original evidence manifest remain immutable. Preview state lives separately;
it does not rewrite unverified acceptance entries or assert deployment readiness. The current
deployment record is not a complete historical release/event ledger.

## Isolation and browser behavior

`preview-server` uses a minimal Python image, an unprivileged user, a read-only root and
preview volume, dropped capabilities, process/memory/CPU limits, and its own Docker network.
The entrypoint first installs IPv4/IPv6 firewall rules inside the container's network
namespace, then drops its UID and every capability, including the bounding set. Startup
fails before serving if firewall setup fails. Only replies and IPv4 loopback connections
are allowed outbound; public/private destinations and Docker's DNS forwarder are blocked.
A normal bridge is required because this Docker engine omits published ports on internal
networks. The server has no Django application, database, execution workspace, Docker socket,
model login, or operator credentials. It cannot connect to the control plane, execution
daemon, database, or public network. Its request logs omit capability hostnames.

Each identifier has its own browser origin. A response Content Security Policy permits
local scripts, WebAssembly, local Web Workers, and downloads, while blocking cross-origin
resource requests, embedded frames, form submission, popups, and plugins. Browser storage
belongs to that unique preview origin. The separate origin permits `allow-same-origin`
inside the sandbox without sharing Tempo's operator origin. Service-worker script requests
are rejected to prevent persistent interception of expired or revoked previews. Responses
use `no-store`, `nosniff`, no-referrer, and opener/frame isolation; they never set cookies.
Links in Tempo open with `noopener noreferrer`.

The real retained mini-app was tested through image upload, its local ONNX model and
WebAssembly worker, SVG generation, and SVG download. Browser probes also verified blocked
operator/external fetches, service-worker registration, and parent localhost cookie writes.
Embedded YouTube content and other external integrations are intentionally unavailable in
this local preview policy. This target does not support application servers or APIs.

## Verification and operations

`tests/test_previews.py` covers operator authorization/CSRF/scope, evidence rejection,
idempotency, PostgreSQL concurrent starts, missing-storage repair, expiry/revocation,
archive validation, route serving, corruption, and browser response policy.

`scripts/restart-tempo.sh` builds and deploys the fifth service, preserves its volume, and
runs `scripts/check-previews.py`. That probe publishes a temporary dependency-free ZIP,
checks actual serving and revocation, verifies the read-only mount and limited container
configuration, and attempts forbidden network connections. It cleans up its own files and
creates no product, run, deployment record, or model request. The normal deployment backup
includes preview database records; preview files are recoverable from retained artifacts.

Next release work is independent criterion verification, a reviewed production deployment
target, artifact promotion, and rollback. Public preview hosting needs a separate domain,
TLS, access policy, and an explicit network/application contract.
