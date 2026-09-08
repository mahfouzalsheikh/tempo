# Approved plans to checked candidates

Open an approved plan in **Ideas & plans → Set up candidate build**. Choose an existing agent
profile for each role and a parallel task limit, review the required checks, then select
**Build & verify candidate**. Approval itself never dispatches work. Starting a build queues
real coding agents and consumes model tokens and compute.

The default path produces a local, unpublished source candidate. Selecting the React mini-app
build target additionally saves the checked production files and an evidence manifest; see
[static builds](STATIC_BUILDS.md). Neither path opens a pull request, merges, provisions a preview,
or deploys. A successful task
or an agent verifier's conclusion is not independent proof of an acceptance criterion.

## Execution contract

A launch transaction locks the product row and requires the latest approved plan, its exact
digest, and the project configuration digest shown in setup. Duplicate submissions for the
same plan and settings return the same run. Changing those settings requires a new approved
plan revision. An existing queued, running, paused, waiting, or retrying run for the product
must be stopped before another plan can start.

The run saves independent copies of the brief, plan, role-to-profile assignments, parallelism,
and original project execution snapshot. The compiled graph has a separate execution digest.
Workers validate both snapshots and recompile the product contract before hooks or agents run;
changes to catalog rows, later brief revisions, or current workflow configuration cannot
replace the saved execution. Each task receives the complete approved context and its own
assignment, including when an agent session resumes. Contract text is inserted as data, never
compiled as a prompt template.

Implementation tasks receive independent Git repositories and can run concurrently when their
dependencies permit. Planner, integrator, and verifier tasks run exclusively in the integration
checkout. Tempo's existing contribution coordinator imports committed changes and serializes
integration. Conflicts retain the contributing repository and stop advancement. The source
project's agent budgets, runtime/model settings, hook grants, validation policy, and execution
image settings remain pinned. Candidate profiles have no Tempo publication, review, or MCP
tools; runtimes still provide file and command execution within their existing sandbox.

Each plan gets a fresh private workspace. The checkout hook prepares the source repository;
for GitHub projects its origin must match the configured host and repository, without embedded
credentials. Tempo records the clean base commit when the checkout is first prepared, before
agent work. The base is therefore resolved at first execution, not pinned to a remote SHA at
launch. Recovery requires that base to remain an ancestor. The final candidate must also
descend from it. Repository bootstrap, empty/new repository creation, and source selection
at approval time remain separate work.

## Checks and evidence

Execution setup requires enabled `validation.required_checks` with `policy: required`, a Git
checkout hook, and a configured active work state. Applicable or unconditional workflow human gates
block execution because they cannot yet be translated into a product plan. Gates whose incoming
label conditions are all false for the product's immutable labels are shown as inactive in setup.
Unknown conditions remain blocking. The chosen
parallelism cannot exceed the project's configured workflow limit.

After every agent stops and the `after_run` hook finishes, the controller runs the saved required
checks through the existing validation runner. Agents cannot emit accepted validation, review,
publication, or no-change events through this path. Tempo requires a clean integrated commit
and unchanged commit/workspace fingerprint across the checks and mandatory cleanup. A failed
check or changed workspace prevents a candidate checkpoint and stops automatic repair retries.

A `product_candidate` checkpoint records the source SHA, workspace fingerprint, host validation
record, policy digest, required check IDs, brief/plan digests, and execution snapshot digest.
The UI shows this evidence only for a successful `CandidateChecksPassed` run. The default source-only result remains
in the run workspace. Selecting a build target additionally retains a digest-addressed artifact
and links it from the checkpoint.

## Recovery and operation

The plan page puts queued/running/stopped state, individual task status, errors, token usage,
and candidate evidence above the brief. Pending work refreshes automatically while the tab is
visible, preserving focused controls and selected text. Failed updates retain the last status;
terminal work stops polling. **Refresh status** also works without JavaScript. Pause, cancel, and retry use
the existing lease-fenced run controls. Retry preserves the run's contracts and workspace;
completed task checkpoints can be reused. The persisted required-check attempt count remains
bounded by the saved run limit across retries. Retrying failed final checks does not automatically
replan or rerun completed implementation tasks. Use **Repair with an agent** for a scoped build
fix, resolve the retained workspace, or revise and approve a new plan for changed work.
Generic “restart with current configuration” is disabled
for product runs so it cannot discard their approved contract.

### Reviewed build repairs

After the approved agent tasks finish, a stopped run without a checked candidate can receive
an explicit repair assignment. Open **Repair with an agent**, review its failure, commit,
uncommitted changes, failed-check output, saved implementer profile, and remaining attempts.
Describe the fix and list allowed repository paths. Directory paths end in `/`; wildcards,
parent traversal, Git metadata paths, and paths outside the selected mini-app are rejected.
The form requires authentication and CSRF and does not start work until submitted.

Submission locks the product and run, rechecks the current approved plan, other active work,
check budget, and the reviewed source fingerprint. An append-only operator action stores the
assignment, failure, source identity, and original snapshot digests. Duplicate submissions reuse
the same action. Neither the approved plan nor its compiled execution snapshot is rewritten.
Each run permits at most three repair requests; the existing required-check limit still applies.

The saved implementer profile gets one turn in the existing integration checkout, with the
original model/runtime settings and attempt token budget. Repository identity, metadata, and
ancestry checks still apply. Dirty source is allowed only for this reviewed repair entry point;
the agent may inspect and restore generated changes. The resulting commit must be clean,
descend from the reviewed commit, and change only allowed paths. This is a check on the final
source diff, not a filesystem sandbox confining every command to those paths. Runtime sandbox
rules continue to apply. No files are automatically reset by the controller.

The normal graph reuses completed tasks, including its earlier verifier report. Fresh host
checks, artifact capture, and independent acceptance/release gates are still required. A model
turn is not proof that the defect is fixed. Failed or interrupted turns remain in the history;
retries cannot silently repeat them. A new repair review is required. Completed repairs are
reused if subsequent checks need a retry. Historical failed-repair usage remains in run totals.
Successful candidates and build manifests include repair action digests and outcomes, which
the shared artifact gate verifies before download, preview, or publication. Runs that already
have checked candidates cannot be repaired in place, preserving their release evidence.

This adds operator-directed agent repair, not automatic diagnosis and repair approval. Saved
validation images must still be available to the runner; concurrent routing to retained images
and a fresh live benchmark without operator code changes remain follow-up work.

The repair implementation passed the full suite (626 tests, 22 environment-dependent skips),
then 144 focused PostgreSQL execution, persistence, artifact, integration, and usage checks.
After the final source-file guard, 37 PostgreSQL repair/evidence/integration checks passed,
including concurrent requests and a tracked file replaced with a FIFO. Browser checks at
390 and 1440 pixels covered the review form and long failure output without horizontal overflow.

All endpoints require installation-wide operator authentication and cookie-authenticated
mutations require CSRF. Project-level roles and lifetime product cost accounting remain future
work. Existing issue-to-PR runs continue through their original execution path.

The [first live pilot](FIRST_AGENT_PILOT.md) ran five coding tasks, including two concurrent
implementers, and reached a verified local staging release. It required operator build and
environment repairs; the record distinguishes this assisted result from autonomous delivery.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/v1/briefs/{id}/execution` | Current project configuration digest, profiles, parallel limit, checks, and setup blockers |
| `POST /api/v1/briefs/{id}/execute` | Queue a candidate; body requires `mode: "candidate"`, `expected_plan_id`, `expected_plan_digest`, `expected_configuration_digest`, `bindings` for all four roles, and integer `parallelism` |
| `GET /api/v1/briefs/{id}` | Includes durable runs, node status, and candidate checkpoint evidence for the selected plan |

Migration `0015` adds nullable plan linkage and saved product contracts to runs. It does not
rewrite historical issue runs. Tests cover simultaneous private contributions, configuration
reload, retry, host-check failure, cleanup mutation, unrelated history, forged events, stale
launches, authentication/CSRF, and PostgreSQL launch/edit races. Deployment checks read stored
contracts and evidence without starting agents or creating product work.

The React mini-app supports retained builds, independent browser criteria, local preview,
and rehearsed staging publication/rollback. An agent planner that inspects the repository can
propose the same typed contracts; its proposal still requires review. Broader repository
bootstrap and autonomous recovery remain outside this first supported path.
