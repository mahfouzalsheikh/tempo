# Isolated coding runtimes and lifecycle hooks

Compose now launches Codex implementation and review sessions, external JSONL runtimes, and all
workspace lifecycle hooks inside disposable containers. Provider calls and shell processes share
the container boundary. Tracker operations, validation authorization, approvals, and persistence
remain in the control plane and communicate with the runtime over its attached JSONL pipes.

## Container boundary

The launcher uses the immutable `TEMPO_RUNTIME_IMAGE`, falling back to `TEMPO_VALIDATION_IMAGE`.
Deployment preloads the built image into the execution daemon. Missing images, a missing or
conflicting `tempo-agents` network, and container setup failures do not fall back to local commands.
The Configuration page shows the configured execution backend and runtime image.

Each container mounts its task workspace. An agent also receives its own persistent home;
hooks receive a temporary home with no model login. Other workspaces, runtime homes, host Codex
state, Docker sockets, and application credentials are absent. The filesystem is read-only except
for the workspace, private agent home, and a 512 MiB temporary filesystem. Inherited image-volume
paths are masked by read-only mounts.

Project code runs as UID/GID 10001 with no effective capabilities or privilege escalation. A root
watchdog retains only SETUID, SETGID, and KILL. It enforces an agent session lifetime of eight hours
by default, configurable with `TEMPO_RUNTIME_TIMEOUT_MS` up to 24 hours. Hooks use their configured
deadline. Per-turn and stall limits remain active in the orchestrator. Containers have two CPUs,
2 GiB memory with no extra swap, and a 256-process limit.

The [execution network policy](EXECUTION_NETWORK.md) allows public web traffic and selected DNS
resolvers while rejecting the daemon, private infrastructure, sibling containers, and metadata
addresses. Validation retains its stricter `--network=none` boundary.

## Private state and credential grants

The `tempo-agent-state` volume is shared only by the control plane and execution daemon. Its paths
are identical at `/data/agent-state`. Agent homes are derived from workspace, durable run/node
identity, and runtime kind. Review has a separate execution scope. Concurrent nodes receive
different homes. Nodes can also use [private contributor checkouts](CONTRIBUTION_INTEGRATION.md).

Each runtime scope reserves a stable container name before updating its home. A second launch
cannot overwrite an active session's login or reuse that home. Cleanup addresses the immutable
container ID, so a delayed stop cannot remove a replacement session that reused the name.
Normal stop, failed initialization, cancellation, and timeout remove the container; cleanup errors
prevent successful completion. Docker's concurrent auto-removal is handled with bounded retries.

Codex homes receive only the model login cache and selected settings: model, reasoning effort,
personality, service tier, and login/workspace restrictions. The launcher writes through directory
descriptors, rejects directory symlinks, and atomically replaces files without following symlinks.
Host MCP definitions, plugins, features, project trust entries, and arbitrary home files are not
imported. Repository-local skills remain available. Explicit, versioned skill/MCP provisioning
remains a later capability-registry step.

Private login caches survive container replacement, including refreshes made by that node.
A newer host login replaces an older node cache. Shared-account refresh-token rotation can still
require reauthentication and reseeding; a centralized refresh broker is not included. Explicit
API-key grants remain supported and skip copying the host login cache. The file-cache and headless
login approach follows [official Codex authentication documentation](https://developers.openai.com/codex/auth).

Runtime and hook environment grants still use references. Their values are passed through the
trusted Docker client's environment, not its command-line arguments; Docker records the explicitly
granted values in container metadata until removal. Jobs cannot reach that metadata API. Broker
endpoint variables and the validation runner token cannot be granted or used as aliases. Docker
jobs use fixed HOME, PATH, CODEX_HOME, and temporary-directory settings.

Hooks may receive an explicitly configured clone token, as the current workflow requires. They
receive no model login or ambient server environment. Hook output retention is bounded, and
configured credential values are redacted from failure messages. Host Git checks also disable
repository fsmonitor commands and hooks so inspection cannot invoke those execution paths.

## Resume, compatibility, and verification

The launcher preserves each new node's home across retries and restarts, so the existing
`thread/resume` and external-runtime resume protocols can reload their history. The protocol is
described in [official App Server documentation](https://learn.chatgpt.com/docs/app-server).
Historical sessions from the former shared Codex home are not bulk-imported. If a saved thread
cannot be found in its new private home, the existing recovery flow starts a fresh thread using
the retained workspace and recovery prompt. Other nodes' history is not exposed to make it resume.

The former default all-reject approval object is normalized to the supported `never` policy,
which is also the new default. Explicit policies remain configurable. This fixes initialization
with the installed Codex version without requesting approval escalation.

Standalone local development defaults to `TEMPO_RUNTIME_BACKEND=process`. Compose explicitly
selects `docker`; there is no automatic compatibility fallback. Process mode has no container
boundary and must not be described as isolated execution.

Tests exercise real containers running deterministic Codex/external protocol fixtures, private
history persistence, simultaneous isolated homes, denied duplicate launches, repeated old-session
cleanup, hook grants and redaction, deadlines, background-process cleanup, and symlink attacks on
credential seeding. A separate test proves host Git would execute a malicious fsmonitor without
the host override. Enable Docker tests with:

```bash
TEMPO_TEST_EXECUTION_NETWORK=1 \
TEMPO_TEST_VALIDATION_IMAGE="$(docker image inspect --format '{{.Id}}' tempo-validation-runner)" \
  .venv/bin/pytest -q tests/test_workload.py tests/test_execution_network.py
```

Deployment runs the runtime smoke script against the installed services, including initialization
of the real Codex binary and recognition of the configured model login. It does not generate a
model turn or mutate a tracked issue/repository. This establishes protocol and login availability,
not an end-to-end generated-product acceptance result.

## Remaining work

Parallel contributors now have independent repositories and serialized commit integration;
nodes using the issue checkout run exclusively. See [contribution integration](CONTRIBUTION_INTEGRATION.md).
New runs pin their [execution configuration](RUN_SNAPSHOTS.md), including portable model settings
and container defaults. Tool/skill artifact identities, trusted build/release artifacts, and
scoped capability grants remain planned.

After a control-plane crash, a running orphan remains bounded by its in-container watchdog. A
reserved container that never started, or a still-running orphan, blocks reuse of that scope;
inspect and remove the abandoned container before retrying. Lease-aware execution recovery is
not yet implemented. Private homes persist and need a retention/backup policy alongside the
workspace volumes; the deployment script's PostgreSQL backup does not include them.

The daemon remains privileged and trusted by the control-plane network. Public web egress is
broad, and a workload can read any model/clone credential explicitly granted to it. Containers
share the host kernel. Host-side repository inspection and publication are separate trusted
boundaries that still need additional path, object, and resource hardening. This is not a claim
of hostile multitenant isolation.
