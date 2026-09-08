# Disposable validation execution

Compose now runs each validation command in a separate container on the existing execution
daemon. The authenticated runner manages containers; project commands cannot access that daemon.
This is the first execution-isolation slice, not a complete sandbox for the software factory.

## Enforced boundary

- One resolved task directory is bind-mounted at the same absolute path. Sibling workspaces,
  host credentials, the Docker socket, and the runner's process namespace are absent.
- Project code runs as UID/GID 10001 with no effective capabilities and no privilege escalation.
  A trusted root watchdog retains only SETUID, SETGID, and KILL to drop the command's privileges
  and enforce its deadline. Project code cannot signal that supervisor.
- The root filesystem is read-only. Writable temporary files and HOME use a 512 MiB tmpfs;
  inherited application-image volume paths are masked by read-only tmpfs mounts. The task
  workspace remains writable and persists between commands.
- Networking is disabled, including access to Compose services, internet downloads, and metadata
  services. No runner token, tracker credentials, or Docker connection setting is supplied.
- Each command has a two-CPU limit, 2 GiB memory limit with no additional swap, and a 256-process
  limit. The configured deadline is enforced inside the container, even if the runner disappears.
  The runner allows five seconds for Docker startup beyond that deadline.
- Normal completion, timeout, and cancellation remove that command's container. Cleanup failure
  prevents a final passing result. Only the configured output tail is retained by the runner.

Commands requiring network installs must use dependencies already in the task workspace or in
the execution image. Temporary HOME and `/tmp` are new for every command; write persistent
inter-command state inside the task workspace. Checks that previously invoked Docker must be
adapted to this boundary. A future build/preview capability will provide separately scoped access.

## Image identity and deployment

`TEMPO_VALIDATION_BACKEND` defaults to `docker`. `TEMPO_VALIDATION_IMAGE` must be a local immutable
image ID (`sha256:` plus 64 lowercase hexadecimal characters), never a mutable tag. The image
must provide bash, GNU timeout, setpriv, and the project's required toolchain. Tempo's application
image is the initial runtime. Its dependencies are not yet all pinned at build time; its resulting
immutable image identity binds the actual runtime used for a deployed validation attempt.

Run `./scripts/restart-tempo.sh` from a committed checkout. The script builds the image, loads it
into the separate Docker-in-Docker image store, and supplies the same ID to Tempo and the runner.
It then checks the deployed revision, authentication, and actual sandbox execution through both
validation endpoints. Direct `docker compose up --build` does not perform image provisioning.

The host takes its expected image from `validation.runner_image` or `TEMPO_VALIDATION_IMAGE`.
Requests and final results must match it; image mismatch fails closed. It also contributes to the
validation policy digest, so changing the image requires fresh evidence and review. The
Configuration page exposes this identity. Missing images cannot be pulled automatically, and
there is no automatic fallback to host execution.

The runner can now serve current and retained images concurrently. The deployment script reads
verified `WorkflowVersion` and `AgentRun` snapshots after workers drain, including the complete
product contract for product runs. It supplies their immutable validation IDs as the installation
setting `TEMPO_VALIDATION_RETAINED_IMAGES` (a JSON list, at most 256 entries). Invalid snapshots
are reported and excluded; records without complete snapshots cannot authorize an image.
The current default image remains allowed. This setting is runner policy, not a new field in
historical execution snapshots; existing contracts and policy digests remain unchanged.

Each authenticated request selects its exact expected image. The runner checks membership in
that list and confirms the exact ID exists in its local execution daemon before starting a
response stream or command. It passes the selection through that job's container launch and
result; concurrent jobs do not change shared defaults. Unapproved images return HTTP 409
`execution_image_mismatch`; approved but missing images return `execution_image_unavailable`.
Broker process startup failures and inspection timeouts return HTTP 503. Mutable tags and image
substitution are unsupported. The same sandbox, timeout, cleanup, authentication, and result checks apply
to older images.

Retain the execution daemon's image storage while saved runs may need retrying. The catalog
does not download or reconstruct missing images. Restore the exact image from a trusted local
archive into that daemon, then rerun `./scripts/restart-tempo.sh` to refresh permissions. Use
that script when deploying; a plain Compose recreation does not preserve its generated image
settings. Configuration changes selecting another image require a catalog refresh after that
configuration has been saved. Removing a historical image deliberately blocks those runs rather than silently
moving them to a newer toolchain. Image garbage collection and per-run broker authorization
remain separate work.

Deployment checks also run simultaneous sandbox commands on the current image and an available
retained image, asserting overlapping execution times, exact result identities, workspace
isolation, credential denial, and disabled networking. They use disposable directories and do
not retry product runs. A fresh installation with no second local image reports that condition.

The implementation passed 652 full-suite tests (24 environment-dependent skips), 62 focused
image/snapshot/authentication tests (four Docker skips), and all seven real Docker sandbox tests.
Focused tests cover concurrent routing on both endpoints, missing images, immutable IDs,
malformed catalogs, inspection timeout/cleanup, and rejection of tampered saved contracts.

`TEMPO_VALIDATION_BACKEND=process` is an explicit compatibility mode for trusted development and
protocol tests. It has no container isolation and requires the host to omit its expected image.
Local validation without a remote runner also remains a process-based compatibility path.
Neither path should be represented as sandboxed execution.

## Verification and remaining work

Run the normal test suite, plus real Docker probes against a built local image:

```bash
TEMPO_TEST_VALIDATION_IMAGE="$(docker image inspect --format '{{.Id}}' tempo-validation-runner)" \
  .venv/bin/pytest -q tests/test_validation_sandbox.py
```

The probes exercise sibling and credential denial, no effective capabilities, read-only image
paths, network denial, workspace writes, simultaneous commands, timeout, cancellation, and
failure to confirm cleanup. Image identity and mismatch tests do not require Docker.

Compose coding agents and hooks now have their own [container boundary](RUNTIME_ISOLATION.md);
graph nodes still share issue workspaces. A process outside this validation boundary can mutate a mounted workspace or race
path resolution. The runner credential is installation-wide, not bound to a leased task. The
privileged execution daemon still shares the control-plane network, while a
[workload firewall](EXECUTION_NETWORK.md) now blocks access from its job bridges. Containers share a host
kernel; this slice does not establish hostile multitenant isolation. Workspace disk quotas,
global job admission, trusted test harnesses, per-node workspaces, scoped job authorization,
lease-aware execution recovery and stronger execution infrastructure remain planned.
