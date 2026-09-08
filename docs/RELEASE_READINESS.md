# Release readiness assessment

Every saved static build now offers **View release readiness**, from its idea and acceptance
pages. It shows six gates, explains blockers, links to browser checks, and exports a private
JSON assessment with a digest. It never rewrites the immutable build manifest.

`release-readiness-v1` evaluates current evidence on each request:

| Gate | Required evidence |
| --- | --- |
| Build and required checks | Valid retained ZIP, frozen execution contract, candidate checkpoint and passed mandatory validation identities |
| Current approved scope | The build's plan is the latest plan of the latest brief revision, still approved, with exact specification/digest matches |
| Reviewed browser checks | All criteria pass in the latest attempt for the latest approved check plan, with verified artifact/report/image identities |
| Preview health evidence | Persisted checks against the deployed preview; not implemented yet |
| Deployment target and configuration | Named environment, configuration contract, and promotion adapter; not implemented yet |
| Rollback rehearsal | Recorded target-specific rollback evidence; not implemented yet |

The final three gates remain blocked. An available preview link is shown as information, not
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
evidence and bind a release to a named environment and immutable artifact. Target adapters,
release history, health probes, promotion recovery, and rollback rehearsal remain next.

Tests cover stale scope, revoked approval, missing deployment evidence, new browser plans,
tampered reports/artifacts/identities, immutable historical manifests, and authenticated/scoped
read-only exports. The existing preview and acceptance isolation boundaries continue to apply.
