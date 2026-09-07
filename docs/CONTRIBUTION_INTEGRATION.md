# Isolated contributions and controlled integration

Tempo can run graph contributors concurrently in independent Git repositories. Each receives only
its own checkout in its runtime container. Git objects, index, configuration, hooks, and working
files are not shared with peers. The host integrates accepted commits into the issue checkout;
later validation and publication nodes operate on that combined result.

## Configuration

Agent nodes support two workspace modes:

| Mode | Behavior |
| --- | --- |
| `integration` (default) | Uses the issue checkout and runs exclusively within its graph. This includes existing workflows, validators, and publishers. |
| `isolated` | Starts from a recorded clean commit in a private repository. May overlap with other isolated contributors, up to `max_parallel_nodes`. |

Isolated contributors must use `completion: turn` and explicit read-only native tool bundles.
The only available host tool is `github_api`; use an empty list for no host tools. Set
`allow_all: false` explicitly. Contributors can use their runtime's local coding and test tools,
but cannot claim the combined validation gate or publish/complete the issue. Tempo also checks
this restriction at runtime. Configure required checks for the target project before enabling
publication, as described in [validation policy](VALIDATION_POLICY.md).

For example, add these definitions to a project's configuration, retaining its tracker,
runtime/model settings, and required validation checks:

```yaml
tool_providers:
  source_reads:
    kind: tempo
    allow_all: false
    tools: [github_api]
  delivery:
    kind: tempo
    allow_all: false
    tools: [github_api, project_validation, github_publish, tempo_complete]
agents:
  contributor:
    completion: turn
    tool_providers: [source_reads]
  integrator:
    completion: publication
    tool_providers: [delivery]
workflow:
  max_parallel_nodes: 2
  require_publication: true
  nodes:
    - id: server
      agent: contributor
      workspace: isolated
      prompt: Implement the server against the agreed API contract. Commit your changes.
    - id: client
      agent: contributor
      workspace: isolated
      prompt: Implement the client against the agreed API contract. Commit your changes.
    - id: verify-and-publish
      agent: integrator
      workspace: integration
      prompt: Check the combined application, run required validation, then publish.
  edges:
    - {from: server, to: verify-and-publish}
    - {from: client, to: verify-and-publish}
```

Define the common interfaces before parallel implementation. Each contributor in a batch starts
from the same integration head. Integration happens in completion order, under both an in-process
lock and a filesystem lock shared by controllers for the issue. A later batch sees all accepted
changes from earlier batches. Keep all necessary contributors upstream of the final validation
and publication node. Human gates and joins also run exclusively.

The configuration screen identifies private and integration checkouts. Live run steps show the
integrated commit when available. Run-node output and durable `contribution_state` checkpoints
record the workspace, base commit, contribution commit, integration commit, and runtime output.

## Acceptance and recovery

1. Create a standalone repository from the clean integration commit. Copy Git objects without
   importing remote credentials, hooks, global settings, alternates, or writable hardlinks.
2. Run the contributor in its private checkout. Retries continue that node's existing checkout
   and private runtime home, preserving unfinished work. This slice does not create a fresh
   repository for every retry attempt.
3. Stop the runtime and its container before inspecting its output. Require a clean committed
   checkout whose history descends from the recorded base. Compare actual blobs and modes,
   independently of index flags. A completed model turn alone is not accepted work.
4. Persist the accepted commit and runtime output. Merge it in a disposable host-controlled
   repository. Only object data is imported; unsupported Git configuration, linked metadata,
   external object stores, submodules, and repository path redirection are rejected.
5. Persist the exact merge intent before fast-forwarding the issue checkout. Recheck lease
   ownership before mutation, confirm the resulting clean commit, then persist completion.
   Integration revokes any previous publication validation authority. It is not a test pass.

On recovery, Tempo reconciles accepted and integrating contributions before starting graph work.
It recognizes an already-applied candidate and does not rerun its agent or create another merge.
Recorded integrated commits must remain ancestors of the integration head. A dirty, divergent,
missing, or unrecorded workspace stops instead of being reset or overwritten. New runs also use
[saved execution snapshots](RUN_SNAPSHOTS.md), so later node configuration edits do not change
their recovery graph. Older runs without a complete snapshot require explicit migration.

Conflicts stop the run for operator attention and retain the original contribution. Earlier
successful integrations remain intact; a conflicting merge never changes the issue checkout.
Resolve the conflict in the integration checkout, commit the resolution, and retry to import the
same pinned contribution. Do not amend an accepted contributor commit in place. There is not yet
a conflict-resolution editor or an automatic repair-agent loop. After resolution, the downstream
node must validate the complete application before publication.

Standalone process mode does not enforce filesystem isolation. Production uses Docker. Durable
recovery requires the persistence store; an unrecorded leftover from a nonpersistent execution
is retained for operator inspection.

## Storage and limitations

Contributor repositories live under a hidden sibling of the issue workspace, scoped by run and
node hashes. The parent directory is never mounted into contributor containers. Workspace removal
also removes that issue's contributor storage. Private runtime homes have their own retention
policy; PostgreSQL backups alone do not back up workspace or agent-state volumes.

This is a Git source handoff. Ignored build output, installed dependencies, LFS content, previews,
release artifacts, acceptance reports, and service databases are not copied between contributors.
Dependencies must be installed in each checkout or supplied through an approved execution image.
The integration path deliberately supports a small Git configuration allowlist; custom filters,
merge drivers, config includes, worktrees, and alternate object stores need a future isolated
import implementation. Full-history object copies cost more storage than worktrees.

The file lock assumes one shared local workspace filesystem with working POSIX locks. This is
not a distributed worker pool, a per-job capability broker, or atomic fencing of every filesystem
effect. Runtime-neutral review, immutable complete run snapshots, bounded automatic conflict
repair, resource limits on host Git inspection, independent acceptance harnesses, and deployable
release artifacts remain on the factory plan.

## Verification

`tests/test_integration.py` exercises parallel work, combined downstream checks, persisted Git
identities, conflicting edits, dirty and rewritten candidates, metadata redirection, lease loss,
and interrupted checkpoint recovery. The Docker probe starts simultaneous container writers,
proves that peers and the integration checkout are invisible, integrates both commits, and
replays the results without changing the head:

```bash
docker compose exec -T tempo python < scripts/check-contribution-integration.py
```

The deployment script runs this probe after its runtime, validation, and network checks. It uses
disposable repositories and no model turns or external issue/publication mutations.
