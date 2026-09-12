# Agent providers, accounts, and team configuration

Status: registry foundation implemented 2026-09-12. Named connections, staff-only forms/API,
protected Codex login-file provisioning, project grants, disable controls and audit history are
available. Runtime selection, provider-verified connection checks and Claude integration remain
planned. See [the registry guide](AGENT_ACCOUNT_REGISTRY.md) for this slice's exact limits.

Operators should be able to connect one or more Codex accounts, one or more Claude accounts,
and subsequently other supported providers, then choose which accounts and agent profiles work
on each project. Start with an **Agents & accounts** page and an understandable setup flow.

Tempo already has runtime/model/tool provider definitions, agent profiles, role bindings at
product launch, credential references, and isolated agent homes. It currently provisions Codex
login state from one configured installation source. External JSONL bridges provide an extension
point; they do not establish a tested native Claude integration or multi-account login support.
Extend these foundations instead of introducing another workflow engine.

## User experience

1. Choose a provider and name the connection, such as “Codex development” or “Claude review”.
   Offer only authentication methods implemented and verified for that provider adapter.
   Explain whether the connection uses a subscription login or separately billed API access.
   A provider appearing in the catalog must not imply that every authentication method works.
2. Connect using the provider's supported flow or select a provisioned secret reference.
   Report authentication, runtime availability, and project authorization separately. Show
   pending, ready, needs reconnection, unavailable, draining, and disabled states with next actions.
3. Allow multiple separately named connections to the same provider. Display a safe account
   identifier, authentication method, available capabilities, and last successful connection check.
   Never display tokens or copy authentication files into editable workflow JSON.
4. Grant each connection to selected projects. Assign profiles to planning, implementation,
   integration, and verification, with a runtime, account, model, tools, and task capabilities.
   Show the resulting team before starting work, including any incompatible assignments.
5. Configure maximum concurrent sessions per account and optional approved fallback accounts.
   With no available capacity, queue work and explain why. Fallback is opt-in, restricted to
   authorized accounts and equivalent task capabilities, and recorded as an execution decision.
6. Show the account actually used on each task and attempt, together with usage, limits, and
   provider errors. Allow reconnect, disable, and removal without losing historical attribution.

Example intended assignment: two implementation profiles use two Codex connections while an
independent verifier uses a Claude connection. An operator can also run every role through one
account. Accounts, runtime implementations, models, and role instructions are separate choices.

## Data and execution boundaries

| Record | Responsibility |
| --- | --- |
| Provider adapter | Supported authentication modes, runtime protocol, model discovery, capabilities, health and normalized failures |
| Account connection | Stable ID, organization/owner, provider identity, safe label, authentication reference, lifecycle status, credential generation and capacity |
| Project grant | Which project may use which connection; administrative connection access is separate from execution access |
| Agent profile binding | Runtime, model selection, connection ID, tools, capabilities and optional authorized fallback order |
| Account lease | Atomic reservation tied to run/task/attempt and worker lease, expiry, release and reconciliation |
| Attempt attribution | Selected account, adapter/version, model, credential generation, fallback reason, usage and redacted outcome |

Store secret references in the database and run snapshots; keep credentials in a restricted
credential store. Provider-specific authentication sessions need isolated storage per connection,
with separate runtime homes per task. Define refresh synchronization so concurrent tasks cannot
overwrite another connection's login state. Only the selected account's credentials may enter a
task. Validation containers must continue to receive no model-account credentials.

Snapshot account identity and routing policy for new runs through an explicit contract version.
Credential rotation may update the credential generation for the same verified provider account;
record that event. Replacing an account identity must require a new connection and reviewed
binding. A disconnected account must not silently resolve to the installation's default login.
Revocation is a current authorization check even for a historical snapshot.

Use account leases across projects and processes, including retries and reviewers. A rate limit
must retain the provider's retry guidance and queue or follow the approved fallback policy.
Account selection must not be designed to evade provider restrictions. Never resume a provider
thread through a different account; an approved fallback starts a new attributed session from
retained task context. Distinguish task concurrency from provider quotas and monetary budgets.

Disabling prevents new leases. Offer draining for active work and an explicit stop/revocation
operation where supported. Preserve connection tombstones for historical runs. Audit connection,
grant, binding, rotation and fallback changes without recording secrets.

## Delivery sequence and acceptance

1. **Account registry and Codex selection.** Add additive models, permissions, redacted forms/API,
   project grants, connection checks, and account selection through isolated runtime provisioning.
   Start with an explicitly documented authentication mode; verify the pinned provider protocol
   before promising an embedded login flow. Keep the current single-account setup usable through
   an explicit legacy connection. Prove two connections can run concurrently without credential
   or session crossover, and that disabled or unauthorized connections fail before model work.
2. **Claude adapter and mixed teams.** Implement and document its supported authentication modes.
   Test start, completion, cancellation, error classification, usage, tool boundaries and recovery
   against the same runtime contract. Route independent review through the runtime interface.
   Demonstrate Codex implementation plus Claude verification with attributable evidence. Unsupported
   capabilities must be visible blockers, not silently substituted behavior.
3. **Account scheduling and recovery.** Add atomic shared capacity, rate-limit cooldowns, lease
   cleanup, rotation, draining and explicit fallback. PostgreSQL process-race tests must prove
   no capacity oversubscription and no new dispatch after revocation. Test account expiry during
   a turn, worker death, duplicate refresh, provider outage and cross-account resume rejection.
4. **Guided setup and evaluation.** A second operator connects multiple accounts, assigns a team,
   diagnoses a failed connection and completes a bounded product run from the UI. Retain actual
   per-task account/model attribution and provider-reported usage; display unknown costs as unknown.

The [second supervised mini-app pilot](SECOND_AGENT_PILOT.md) now has an accepted candidate and
passed readiness evidence without operator source edits and was published on 2026-09-12. The
registry and native Codex routing now cover named connections, project grants, profile assignments,
account-specific task homes, immutable bindings and durable session attribution. Next verify a
bounded real multi-account run, add the Claude adapter, and complete shared scheduling/refresh
recovery alongside CI work. See [current routing behavior and limits](AGENT_ACCOUNT_REGISTRY.md).
Do not defer account configuration until broad MCP integrations or distributed worker scaling.
The complete mixed-provider feature remains open until the Claude and account-isolation gates pass.

Related: [factory sequence](SOFTWARE_FACTORY_PLAN.md), [product experience](PRODUCT_EXPERIENCE.md),
[credential boundaries](CREDENTIALS.md), [runtime isolation](RUNTIME_ISOLATION.md).
