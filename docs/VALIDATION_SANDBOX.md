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
