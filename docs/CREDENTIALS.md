# Credential references and subprocess environments

Tempo retains tracker credentials as environment references in configuration. It resolves them
only when constructing the host-side GitHub adapter. Workflow snapshots therefore contain
`$GITHUB_TOKEN`, not its value. Custom reference names remain associated with the adapter so
they can also be denied to coding agents and local validation processes.

```yaml
tracker:
  kind: github
  provider:
    repo: acme/application
    token: $TEMPO_PROJECT_GITHUB_TOKEN
    review_token: $TEMPO_PROJECT_REVIEW_TOKEN
  active_states: [open]
  terminal_states: [closed]
```

References use exactly `$NAME`, where NAME contains letters, digits, or underscores and does
not begin with a digit. `${NAME}`, interpolation, and inline tracker credentials are rejected.
An explicitly configured publication-token reference must resolve to a nonempty variable when
the tracker is built. An unset review-token reference remains optional. With no token configured,
the adapter retains its optional `GITHUB_TOKEN` fallback for public-repository reads. Credential
rotation takes effect when the adapter is rebuilt, normally on service restart.

## Explicit subprocess grants

Coding runtimes, hooks, local validation, remote validation subprocesses, and host Git inspection
commands receive only these ambient variables when present:

`PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `TMPDIR`.

Missing PATH falls back to the operating system's default executable path. Shells run without
login/profile startup files. Unknown environment variables, shell functions, `BASH_ENV`, proxy
credentials, database credentials, Git credential configuration, SSH-agent sockets, and provider
API keys are no longer inherited implicitly.

An operator can explicitly grant variables to `hooks.environment`, `codex.environment`, or a
named `runtime_providers.<name>.environment`. Each mapping is a subprocess variable name to an
environment reference. All configured references are required and resolved immediately before
launch; configuration and runtime-definition records retain references only.

```yaml
hooks:
  environment:
    GITHUB_TOKEN: $TEMPO_PROJECT_GITHUB_TOKEN
  # Existing clone/fetch scripts can now use this specifically granted token.
codex:
  environment:
    OPENAI_API_KEY: $TEMPO_CODEX_API_KEY
    # Include this only when using a nondefault Codex state directory:
    CODEX_HOME: $TEMPO_CODEX_STATE_DIRECTORY
runtime_providers:
  specialist:
    kind: openai-agents
    command: /opt/bridges/specialist
    environment:
      PROVIDER_API_KEY: $TEMPO_SPECIALIST_API_KEY
```

For Codex runtimes, provider-specific grants override the same variable in `codex.environment`.
External runtimes receive only their own provider grants. Do not configure an API-key grant when
using login-based authentication without that key; HOME-based authentication files still work.
Granting a key to a coding runtime also makes it available to that runtime's child commands.

Coding-runtime grants reject both source and destination names matching the tracker's credentials
or the standard control-plane credentials (`DATABASE_URL`, `POSTGRES_PASSWORD`, `TEMPO_POSTGRES_PASSWORD`,
`DJANGO_SECRET_KEY`, `TEMPO_ADMIN_PASSWORD`, `GITHUB_TOKEN`, and `GITHUB_REVIEW_TOKEN`). Hooks may
receive tracker credentials for clone/fetch. Shell startup and dynamic-loader injection variables
are rejected even when explicitly granted. Literal occurrences of granted hook values are
redacted from hook failure output before logging or raising an error.

Validation subprocesses use the baseline environment, with an explicit Docker endpoint on the
remote runner. Runtime grants do not propagate to validation, and job payloads do not carry secrets.
The runner authenticates requests with a separate credential and uses `DOCKER_HOST` only in
its trusted container-management process. Disposable validation containers receive neither value
and have no network or Docker socket. See [validation isolation](VALIDATION_SANDBOX.md) and
[runner authentication](ACCESS_AND_DEPLOYMENT.md).
Per-job capabilities and additional scoped validation credentials remain planned.

## Upgrade

1. Replace any inline `tracker.provider.token`, `api_key`, or `review_token` with an environment
   reference. Keep secret values in the service environment, outside versioned workflow files.
2. Add explicit grants for clone/fetch hooks and runtime API keys or custom runtime directories.
   The checked-in workflow already grants `GITHUB_TOKEN` to its hooks. Its API-key grant is
   commented out so the login-based configuration does not require an API key.
3. Run `python manage.py migrate` using the deployment's Python environment before restarting
   Tempo. Migration `0011_redact_workflow_credentials` replaces literal values in the three known
   tracker credential fields of historical `WorkflowVersion.config` snapshots with `[REDACTED]`.
   It preserves references, other metadata, version identities, and checksums. It is idempotent;
   reversing the migration does not recover removed values. Snapshots are audit metadata, not
   the live tracker credential source.
4. Rotate credentials that older releases stored in snapshots. The migration does not erase
   database backups, transaction logs, exported records, or previously captured application logs.

This is an environment and configuration boundary, not a process sandbox or vault. Arbitrary
prompts, scripts, provider settings, agent output, and files are not a safe place to store secrets
and are not covered by the snapshot migration. HOME/PATH remain trusted operator inputs; mounted
credential files, shared process privileges, writable configuration, Docker sockets, and network
access require the planned task isolation and capability broker. The existing `CredentialReference`
database model is not yet a vault integration. No claim of complete secret isolation is made.
