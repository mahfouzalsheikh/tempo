# Controlled GitHub publication

Tempo exposes three separate GitHub tools:

| Tool | Authority |
| --- | --- |
| `github_api` | GET/HEAD access to repository metadata, issues, PRs, source, commits and refs in the configured repository |
| `github_publish` | Publish the accepted local commit to the durable run branch and create or recover its PR; accepts only `title` and `body` |
| `github_comment` | Post an idempotent text comment to the run's source issue; accepts only `body` |

Generic API writes are denied even after validation. Read paths must be canonical and repository
scoped; encoded paths, traversal, query strings embedded in paths, and operational resources such
as repository secrets and collaborators are rejected. Use `params` for read query parameters.
Commenting is independent of the publication gate. Configured operator approval policy and lease
ownership checks still apply to writes. Merge, review submission, issue state, and dispatch-label
changes remain control-plane operations.

## Candidate and target

Before agent execution, Tempo records the repository, default base branch, base commit, branch
`tempo/run-<run-id>`, and PR marker in a `publication_state` checkpoint. Retries reuse this target.
Changing the configured repository does not redirect an existing run's publication. A remote
default branch and its commit must exist; empty repositories are not supported by this path yet.

The agent commits locally and runs `project_validation`, then calls `github_publish`. Publication
requires a clean committed checkout, the current accepted fingerprint when validation is enabled,
and ancestry from the recorded task base. Subsequent updates must also descend from the last
published commit. The fingerprint includes framed file paths, executable modes, symlink targets,
and file content. Clean-candidate checks compare it with the actual commit blobs, with Git replace
objects disabled. Index flags and cached timestamps cannot substitute different workspace bytes
for the published source.

Ignored build output remains outside validation fingerprints. This path requires a complete Git
object history and checkout; shallow repositories, sparse checkouts, and submodules are not
supported publication inputs. Independent build sandboxes and trusted required checks remain
separate planned work.

`tempo_complete` requires a clean tree identical to the recorded base tree, including when
validation is disabled. A concrete reason is still required. This proves the absence of a source
delta; it does not prove that every product requirement is satisfied.

## Push and interruption recovery

Tempo writes a durable intent containing the candidate SHA, validation fingerprint, and expected
previous remote SHA before starting a push. It copies the commit's objects into a new temporary
bare repository. Credentials are provided only to the final push subprocess through its
environment; the agent checkout's remotes, push settings, hooks, and credential helpers are not
used for that push. HTTP redirects are disabled and subprocess errors are redacted.

Exactly one ref is sent. An explicit expected SHA protects the update; an empty expected value
requires that the branch does not exist. An ancestry check also prevents rewriting published
history. The compare-and-update behavior uses Git's explicit
[`--force-with-lease=<ref>:<expect>` option](https://git-scm.com/docs/git-push).

After a transport error or restart, Tempo compares the remote ref with the saved intent. A matching
candidate is recorded as pushed without resending. A pending intent can be retried with the same
candidate and expected predecessor. A different candidate remains blocked until the previous
outcome is reconciled. Unexpected branch movement is a conflict, including a preexisting branch
with no recorded publication authority.

Before creating the PR, Tempo saves its title/body intent. It searches by the fixed head branch
and verifies the run marker, head repository/ref/SHA, base repository/ref, and open state. A lost
creation response is reconciled using the same checks. Subsequent calls reuse the PR and preserve
its initial title/body. Closed, unrelated, or ambiguous PRs block publication. PR requests follow
the [GitHub pull-request API](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request).

The host recognizes a publication event only when its result matches the durable publication
state. Reading an arbitrary PR through `github_api` does not complete a new GitHub run.

These are publication-specific recovery guarantees. Comments use markers, while other external
actions still need a general side-effect ledger. A lease check cannot recall an HTTP request or
push already in flight. Expected-SHA updates limit branch races, but independent sandbox and
credential isolation remain necessary before expanding worker authority.

## Upgrading existing workflows

The example `WORKFLOW.md` includes the new tool names. Existing database-backed tool-provider
allowlists retain their saved configuration: add `github_publish` to implementation/publisher
profiles and `github_comment` where progress comments are desired. Keep `github_api` for reads.
Do not give publication tools to roles intended only to inspect code. Review's built-in tools
include `github_publish` so the reviewer can publish validated fixes.

Update custom prompts that request raw GitHub ref/commit writes or shell pushes to request
`github_publish` instead. Revalidate retained workspaces: fingerprints from the earlier format
are deliberately invalidated. Legacy agent-selected PR branches are not automatically adopted by
the new publisher; complete their existing review/handoff or start a new run using the controlled
branch. No database migration is needed; publication state uses fenced checkpoints.

The external JSONL adapter still lacks the complete host-tool request protocol. The integrated
publish tool currently runs through the Codex adapter; adding other runtimes requires that
protocol and its conformance tests.
