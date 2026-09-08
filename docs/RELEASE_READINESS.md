# Release readiness assessment

Every saved static build now offers **View release readiness**, from its idea and acceptance
pages. It shows six gates, explains blockers, links to browser checks, and exports a private
JSON assessment with a digest. It never rewrites the immutable build manifest.

`release-readiness-v3` evaluates current evidence on each request. Earlier version 1 and 2
exports remain historical assessments; version 3 adds reviewed staging configuration:

| Gate | Required evidence |
| --- | --- |
| Build and required checks | Valid retained ZIP, frozen execution contract, candidate checkpoint and passed mandatory validation identities |
| Current approved scope | The build's plan is the latest plan of the latest brief revision, still approved, with exact specification/digest matches |
| Reviewed browser checks | All criteria pass in the latest attempt for the latest approved check plan, with verified artifact/report/image identities |
| Preview health evidence | A passing HTTP probe of the current preview generation, every inventory file and browser headers, no older than five minutes |
| Deployment target and configuration | Latest verified approval for the exact artifact and named local staging target, static runtime requirements, serving and rollback policies |
| Rollback rehearsal | Recorded target-specific rollback evidence; not implemented yet |

The rollback gate remains blocked. The target gate can pass after configuration review;
the promotion coordinator has not yet been connected. An available preview link is information, not
health evidence. Passing every reviewed browser journey proves that coverage only; it cannot
substitute for a target, health checks, or rollback. A newer brief or plan blocks the scope gate,
while historical candidate evidence remains intact. A newer browser plan or attempt clears
the previous acceptance pass from the current assessment. Corrupt evidence fails closed.

The authenticated GET endpoint is
`/ideas/{brief}/runs/{run}/artifacts/{artifact}/readiness/`; append `?format=json` to export.
It scopes all IDs together, rejects writes, and is excluded from caches. The assessment includes
artifact/source/snapshot/plan identities, current browser plan/attempt references, verified
file evidence where applicable, gate outcomes, evaluation time, and an assessment digest.
The digest detects content changes; it is not a cryptographic attestation or release approval.
No preview bearer link is included in the export.

This is a read-time, point-in-time assessment, not a transactional promotion authorization.
It is not persisted as a release record. The future promotion path must lock/recheck current
evidence and bind a release to a named environment and immutable artifact. Release history, target health probes, promotion recovery, and rollback rehearsal remain next.
The first adapter and configuration UI support [local staging](LOCAL_STAGING.md); configuration
approval reserves an address and does not activate a deployment.

## Preview health checks

Start a local preview from the idea, then select **Check preview health** in its readiness page.
The authenticated, CSRF-protected POST at the artifact's `/health/` endpoint queues a durable
`PreviewHealthAttempt` (migration `0019_preview_health_evidence`). It binds the exact artifact,
manifest, preview generation, expiry, connection settings, and published-copy file identity.
An expected generation rejects stale-page requests; request keys and artifact row locks prevent
duplicate active checks. History is retained across renewed preview generations.

The existing acceptance worker processes HTTP health jobs alongside isolated browser jobs,
one of each per loop, without starving either queue. It claims with a deadline and lease,
revalidates the candidate and publication, then fetches `/` and every exact inventory path from
the configured preview service. The host/port come only from operator configuration, never
from a brief, project file, redirect, or form. Requests carry only the preview Host identity
and identity encoding; proxy variables, cookies, and authentication credentials are not used.

The trusted `preview-health-v1` probe verifies HTTP 200, exact lengths and SHA-256 hashes,
content types, and required CSP/cache/referrer/opener/frame/nosniff headers. Redirects,
duplicate response headers, cookies, compressed bodies, partial files, and policy mismatches
fail. Reads stream through bounded buffers. A 45-second watchdog shuts down an active socket,
including slow response headers; per-connection I/O also has a three-second timeout. Standard
system DNS resolution applies to the operator-configured service name. The persisted lease
has a 60-second deadline; crashes or late completions require a fresh check.

Before accepting a pass, the worker locks the artifact, deployment, and attempt in that order,
rechecks identity and lease ownership, validates complete probe results, and records the worker
build revision, checked time, and report digest. A stop or renewal racing completion cannot
transfer evidence to the replacement. The summary verifies the report and timestamp, current
generation, publication metadata and file stat identity. New attempts clear the current pass;
failed, interrupted, expired, missing, or changed evidence does not inherit an older pass.

Health evidence expires after five minutes or preview expiry, whichever comes first. It is an
observation of serving health, not continuous availability or proof that an external browser can
reach the published loopback port. The worker tests the actual serving process over its private
Docker route; the deployment smoke test separately exercises the published host port. Stopping
the service after a successful check can leave a fresh observation until its expiry or a new
failed check. A future promotion transaction must perform fresh health verification.

The worker receives a read-only preview volume and joins the preview ingress network. The
preview server retains its outbound-deny firewall and zero-capability serving process; live
probes verify that it cannot initiate connections to the worker or control-plane services.
Browser acceptance containers remain offline with their single read-only input mount.

A disposable PostgreSQL/browser rehearsal checked the retained React mini-app's 26 files
(69,988,236 bytes), recorded a passing health receipt, and showed four passing gates after its
image-to-SVG acceptance journey. Renewing the preview immediately removed the old health pass;
a new check established evidence for the replacement. The mobile page had no horizontal overflow.
These rehearsals use existing committed source and create no production product records.

Tests cover stale scope, revoked approval, missing deployment evidence, new browser plans,
tampered reports/artifacts/identities, immutable historical manifests, and authenticated/scoped
read-only exports. The existing preview and acceptance isolation boundaries continue to apply.
