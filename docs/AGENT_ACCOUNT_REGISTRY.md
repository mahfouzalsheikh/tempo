# Agent account registry

The first account-management milestone adds **Agents & accounts** at `/agents/accounts/` and a
staff-only `/api/v1/accounts` API. Operators can name multiple Codex connections, grant active
projects in the same organization access, disable a connection, inspect recent audit events and
check the stored login file. Every mutation checks the current revision; stale forms fail without
replacing newer changes. Staff is currently an installation-wide administrative role.

This is a registry foundation. Grants and disabling are saved for the upcoming runtime integration;
they do not reroute or stop existing agents. The API explicitly returns `runtime_routing:
"not_available"` and `authentication_verified: false`. Profiles, historical execution snapshots and
existing installation authentication remain unchanged. No model turns are triggered by this page.

## Supported login setup

This slice supports provisioned **Codex ChatGPT file credentials** and an explicit reference to the
**existing installation login**. It does not implement a browser/device login flow, API-key
connections, credential replacement, token-refresh synchronization or Claude authentication.
OpenAI documents separate ChatGPT subscription and usage-billed API authentication, file storage
under `CODEX_HOME`, and copying the login cache to a trusted headless installation:
[official Codex authentication documentation](https://learn.chatgpt.com/docs/auth).

For the current installation login, add a connection with that source and select **Check stored
login**. This reads its private `auth.json` without copying, overwriting or signing out that login.
It does not automatically grant any project access.

For a separate login, add a **Provisioned Codex ChatGPT login** connection. Its UUID and revision
appear under connection details. An administrator supplies a private Codex `auth.json` through a
secure server-side channel, then runs the installed management command as the Tempo service user:

```sh
python manage.py provision_codex_account \
  --account CONNECTION_UUID --revision 1 \
  --auth-file /private/path/auth.json --operator STAFF_USERNAME
```

In Docker, invoke this with `docker compose exec -T --user 10001 tempo` after making the file
available to that user with mode `0600`. Supply only the path; never paste credential contents into
the application, command arguments, workflow files, tickets or chat. Remove the temporary source
using your installation's credential-handling procedure after successful provisioning.

The command copies only the credential file to `TEMPO_ACCOUNT_CREDENTIAL_ROOT`, under a UUID-derived
filename, with mode `0600`. Compose configures `/data/account-credentials` in a dedicated volume
mounted only into the control-plane service. This volume is not mounted into agent, validation,
acceptance, preview or release containers. Database rows and audit events contain metadata only.
The ordinary database backup does not back up this separate credential volume.

Provisioning refuses links, non-regular files, group/world-readable files, files larger than 1 MiB,
unsupported cache formats and overwrites. A successful import is immutable in this slice. If a
filesystem write succeeds but its database transaction fails, the orphan file remains private and
blocks another import; an administrator must reconcile it. No automatic overwrite is attempted.

## Meaning of a check

**Login stored** means the cache has supported fields and its locally reported workspace/user
reference matches the connection's recorded fingerprint. It does not verify signatures, token
expiry, subscription entitlement, provider reachability or available runtime capacity. The page
labels provider/runtime verification as pending. A missing, unreadable or changed identity produces
**Login unavailable** without displaying raw errors or credentials. Replacing an account identity
requires a new connection. Only a short fingerprint is displayed, not tokens or raw account claims.

## API

Authenticated staff can GET `/api/v1/accounts`. POST accepts exactly one of these shapes:

```json
{"action":"create","organization_id":1,"label":"Codex review","auth_mode":"codex-file"}
```

```json
{"action":"settings","account_id":"CONNECTION_UUID","revision":1,"disabled":false,"project_ids":[1]}
```

```json
{"action":"check","account_id":"CONNECTION_UUID","revision":2}
```

Cookie-authenticated requests require CSRF protection; valid bearer authentication uses the existing
API policy. Responses are private/no-store. Unsupported fields, including credential contents,
are rejected. Grant removal retains inactive grant rows and an audit event; records are not deleted.

## Next integration gate

Implement saved account/profile bindings, live grant/revocation checks before dispatch, isolated
credential provisioning per task and account-attributed attempts. Prove two distinct accounts can
run concurrently without credential/session crossover, and reject unauthorized or disabled
connections before model work. Current tests prove registry and file-storage separation only;
they do not establish multi-account runtime isolation or a mixed Codex/Claude team.

## Validation for this slice

The full suite passed **711 tests with 25 environment-dependent skips**. All **17 registry tests**
passed against an isolated PostgreSQL instance, including a simultaneous-edit race. After refining
the UI to show disabled and login-check states independently, focused registry and snapshot tests
passed **36 tests with one PostgreSQL skip**. Ruff, Django checks, migration consistency and Compose
configuration checks passed. A separate browser fixture verified staff sign-in, saved disable
settings, missing-login feedback and layouts at 390px and 1440px with no horizontal overflow.
These are registry checks, not evidence of multiple real provider sessions.
