# Execution network policy

Tempo's dedicated Docker daemon now installs a workload firewall before it accepts jobs. This is
a prerequisite for moving coding agents and hooks into containers: access to an unauthenticated
Docker API would otherwise let a job mount the shared workspace root or start a privileged container.

The policy is baked into `Dockerfile.execution` and installed inside `project-runner` by
`scripts/execution-daemon.sh`. Updating source files does not change the running policy; deploy
the rebuilt image to apply changes. It does not
change the host firewall or other Compose projects. The daemon and wrapper explicitly use the
same nft-backed iptables implementation. Readiness checks both Docker and the required IPv4/IPv6
rules, including the forwarding hook. Restart installs policy before opening the API listener.

## Allowed and denied traffic

The default Docker bridge, Docker-generated bridge interfaces, and the reserved `tempo-agents0`
interface have the following policy:

- Outbound IPv4 TCP ports 80 and 443 may reach public addresses.
- TCP and UDP DNS may reach only 1.1.1.1 and 8.8.8.8. These are also the daemon's default
  container resolvers. Docker's embedded DNS still resolves names on user-defined networks.
- Private, loopback, link-local, carrier-grade NAT, benchmark, documentation, multicast, and
  reserved IPv4 ranges are denied before any port allowances.
- Traffic to the daemon's own interfaces is rejected separately in INPUT, including its Docker
  TCP listener. Blocking only forwarded traffic would miss this route.
- All other forwarded traffic from workloads is rejected. Workload IPv6 traffic is rejected;
  the provisioned agent bridge has IPv6 disabled.

Deployment provisions `tempo-agents` with the fixed bridge interface, inter-container
communication disabled, and a versioned policy label. It refuses a conflicting preexisting
network configuration. The default bridge is protected too, so accidentally omitting the named
network does not expose the daemon or private services.

Public web access is broad: this is not a hostname allowlist or an exfiltration prevention system.
Model credentials explicitly granted to a future agent remain accessible to that agent. Private
package registries, SSH, arbitrary service ports, and IPv6 require a separately designed capability;
they are not exceptions agents can request by changing shell arguments.

## Deployment and verification

Use `./scripts/restart-tempo.sh`. It builds application images, preserves PostgreSQL, drains Tempo
and validation, and saves the database backup before updating the execution service. It then waits
for firewall-aware readiness, provisions the network, loads the validation image, and starts the
application services. Workspaces and existing named volumes are preserved.

The final smoke check starts disposable, unprivileged containers on both bridges. It verifies
that the daemon, a live sibling HTTP server, control-plane services, and metadata addresses are
unreachable; public DNS and an HTTPS TCP connection must still work. These probes never receive
application credentials. Existing validation probes separately prove that validation continues to
use `--network=none`. Probe containers are removed on completion or failure.

To run integration tests in a separate disposable daemon:

```bash
TEMPO_TEST_EXECUTION_NETWORK=1 \
TEMPO_TEST_VALIDATION_IMAGE="$(docker image inspect --format '{{.Id}}' tempo-validation-runner)" \
  .venv/bin/pytest -q tests/test_execution_network.py
```

The tests use the existing local `docker:27-dind` image, a temporary outer network, and a reachable
HTTP canary as a positive control. They also remove a required rule, assert readiness fails,
restart the daemon, and repeat connection probes. They do not alter the running Tempo stack.

## Remaining boundary

Compose now runs coding agents and hooks on this bridge through the
[runtime isolation launcher](RUNTIME_ISOLATION.md). Validation jobs retain their stricter
network-free sandbox. Standalone process compatibility mode remains available for trusted development.

The Docker API still trusts the control-plane network. Mutual TLS or an authenticated execution
broker, task-scoped grants, per-node workspaces, and complete run snapshots
remain planned. The daemon is privileged and containers share the host kernel. Root access to the
daemon can change policy or create a custom network outside the supported bridge configuration;
untrusted workloads must never receive that authority.
