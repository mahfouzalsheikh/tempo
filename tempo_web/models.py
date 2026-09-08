from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.name


class Project(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="projects",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    active = models.BooleanField(default=True)
    max_concurrent_runs = models.PositiveIntegerField(default=3)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__slug", "slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"],
                name="tempo_unique_organization_project",
            )
        ]

    def __str__(self) -> str:
        return f"{self.organization.slug}/{self.slug}"


class Repository(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="repositories")
    provider = models.CharField(max_length=32)
    external_ref = models.CharField(max_length=500)
    clone_url = models.URLField(max_length=1000, blank=True)
    default_branch = models.CharField(max_length=255, default="main")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["project", "provider", "external_ref"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "provider", "external_ref"],
                name="tempo_unique_project_repository",
            )
        ]

    def __str__(self) -> str:
        return self.external_ref


class Environment(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="environments")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    workspace_root = models.TextField(blank=True)
    max_concurrent_runs = models.PositiveIntegerField(default=3)
    policy = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["project", "slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug"],
                name="tempo_unique_project_environment",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.slug}"


class WorkflowVersion(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="workflows")
    name = models.CharField(max_length=255, default="issue-to-pull-request")
    version = models.PositiveIntegerField()
    path = models.TextField()
    checksum = models.CharField(max_length=64)
    config = models.JSONField(default=dict)
    execution_snapshot = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "name", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name", "version"],
                name="tempo_unique_project_workflow_version",
            ),
            models.UniqueConstraint(
                fields=["project", "checksum"],
                name="tempo_unique_project_workflow_checksum",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}@{self.version}"


class CredentialReference(models.Model):
    """Credential metadata only; secret values remain in the configured secret provider."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="credentials")
    name = models.CharField(max_length=255)
    provider = models.CharField(max_length=100)
    secret_reference = models.CharField(max_length=1000)
    scopes = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"],
                name="tempo_unique_project_credential",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}"


class AgentRuntimeDefinition(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_runtimes")
    name = models.SlugField(max_length=100)
    kind = models.CharField(max_length=100)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="tempo_unique_project_agent_runtime"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}"


class ModelProviderDefinition(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="model_providers")
    name = models.SlugField(max_length=100)
    kind = models.CharField(max_length=100)
    model = models.CharField(max_length=255, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="tempo_unique_project_model_provider"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}"


class ToolProviderDefinition(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tool_providers")
    name = models.SlugField(max_length=100)
    kind = models.CharField(max_length=100)
    tools = models.JSONField(default=list, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="tempo_unique_project_tool_provider"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}"


class AgentProfile(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_profiles")
    name = models.SlugField(max_length=100)
    role = models.CharField(max_length=100)
    runtime = models.ForeignKey(
        AgentRuntimeDefinition, on_delete=models.PROTECT, related_name="agent_profiles"
    )
    model_provider = models.ForeignKey(
        ModelProviderDefinition, on_delete=models.PROTECT, related_name="agent_profiles"
    )
    tool_providers = models.ManyToManyField(ToolProviderDefinition, related_name="agent_profiles")
    prompt = models.TextField(blank=True)
    max_turns = models.PositiveIntegerField(null=True, blank=True)
    completion = models.CharField(max_length=32, default="publication")
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="tempo_unique_project_agent_profile"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.name}"


class WorkflowNodeDefinition(models.Model):
    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.CASCADE, related_name="nodes"
    )
    key = models.SlugField(max_length=100)
    name = models.CharField(max_length=255)
    node_type = models.CharField(max_length=32)
    agent_profile = models.ForeignKey(
        AgentProfile,
        on_delete=models.PROTECT,
        related_name="workflow_nodes",
        null=True,
        blank=True,
    )
    prompt = models.TextField(blank=True)
    max_retries = models.PositiveIntegerField(default=0)
    configuration = models.JSONField(default=dict, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["workflow_version", "position", "key"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_version", "key"], name="tempo_unique_workflow_node"
            )
        ]

    def __str__(self) -> str:
        return f"{self.workflow_version}/{self.key}"


class WorkflowEdgeDefinition(models.Model):
    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.CASCADE, related_name="edges"
    )
    source = models.ForeignKey(
        WorkflowNodeDefinition, on_delete=models.CASCADE, related_name="outgoing_edges"
    )
    target = models.ForeignKey(
        WorkflowNodeDefinition, on_delete=models.CASCADE, related_name="incoming_edges"
    )
    condition = models.CharField(max_length=255, default="succeeded")
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["workflow_version", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_version", "source", "target"],
                name="tempo_unique_workflow_edge",
            )
        ]

    def __str__(self) -> str:
        return f"{self.source.key} → {self.target.key}"


class TrackedIssue(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="issues",
        null=True,
        blank=True,
    )
    repository = models.ForeignKey(
        Repository,
        on_delete=models.PROTECT,
        related_name="issues",
        null=True,
        blank=True,
    )
    tracker_kind = models.CharField(max_length=32)
    external_id = models.CharField(max_length=255)
    identifier = models.CharField(max_length=255, db_index=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    state = models.CharField(max_length=100, db_index=True)
    url = models.URLField(max_length=1000, blank=True)
    labels = models.JSONField(default=list, blank=True)
    native_ref = models.JSONField(default=dict, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_synced_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "tracker_kind", "external_id"],
                name="tempo_unique_project_tracker_issue",
            )
        ]

    def __str__(self) -> str:
        return f"{self.identifier}: {self.title}"


class AgentRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        PAUSED = "paused", "Paused"
        WAITING_APPROVAL = "waiting_approval", "Waiting for approval"
        RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    restarted_from = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="successor", null=True, blank=True,
    )
    fresh_workspace_key = models.CharField(max_length=100, blank=True)
    execution_plan = models.ForeignKey(
        "ExecutionPlan", on_delete=models.PROTECT, related_name="runs", null=True, blank=True,
    )
    product_snapshot = models.JSONField(default=dict, blank=True)
    product_snapshot_digest = models.CharField(max_length=64, blank=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="runs",
        null=True,
        blank=True,
    )
    environment = models.ForeignKey(
        Environment,
        on_delete=models.PROTECT,
        related_name="runs",
        null=True,
        blank=True,
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="runs",
        null=True,
        blank=True,
    )
    issue = models.ForeignKey(TrackedIssue, on_delete=models.PROTECT, related_name="runs")
    idempotency_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    attempt = models.PositiveIntegerField(null=True, blank=True)
    priority = models.IntegerField(default=5, db_index=True)
    phase = models.CharField(max_length=100, default="PreparingWorkspace")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )
    workspace_path = models.TextField(blank=True)
    execution_snapshot = models.JSONField(default=dict, blank=True)
    snapshot_digest = models.CharField(max_length=64, blank=True)
    available_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_id = models.CharField(max_length=255, blank=True, db_index=True)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    checkpoint = models.JSONField(default=dict, blank=True)
    feedback = models.TextField(blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    pull_request_url = models.URLField(max_length=1000, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["issue", "-started_at"]),
            models.Index(fields=["status", "available_at", "priority"]),
        ]

    def __str__(self) -> str:
        return f"{self.issue.identifier} · {self.started_at:%Y-%m-%d %H:%M:%S}"


class AgentSession(models.Model):
    run = models.OneToOneField(AgentRun, on_delete=models.CASCADE, related_name="session")
    agent_role = models.CharField(max_length=32, default="implementation")
    session_id = models.CharField(max_length=255, blank=True)
    thread_id = models.CharField(max_length=255, blank=True)
    turn_id = models.CharField(max_length=255, blank=True)
    process_id = models.CharField(max_length=64, blank=True)
    turn_count = models.PositiveIntegerField(default=0)
    last_event = models.CharField(max_length=255, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_message = models.JSONField(default=dict, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)
    thread_input_tokens = models.PositiveBigIntegerField(default=0)
    thread_output_tokens = models.PositiveBigIntegerField(default=0)
    thread_total_tokens = models.PositiveBigIntegerField(default=0)

    def __str__(self) -> str:
        return self.session_id or f"Session for run {self.run_id}"


class RunNode(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        WAITING = "waiting", "Waiting"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="node_runs")
    node_definition = models.ForeignKey(
        WorkflowNodeDefinition,
        on_delete=models.PROTECT,
        related_name="runs",
        null=True,
        blank=True,
    )
    node_key = models.SlugField(max_length=100)
    name = models.CharField(max_length=255)
    node_type = models.CharField(max_length=32)
    agent_name = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=100, blank=True)
    runtime = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempt = models.PositiveIntegerField(default=0)
    dependencies = models.JSONField(default=list, blank=True)
    input = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    checkpoint = models.JSONField(default=dict, blank=True)
    session_id = models.CharField(max_length=255, blank=True)
    thread_id = models.CharField(max_length=255, blank=True)
    turn_count = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)
    thread_input_tokens = models.PositiveBigIntegerField(default=0)
    thread_output_tokens = models.PositiveBigIntegerField(default=0)
    thread_total_tokens = models.PositiveBigIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["run", "id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "node_key"], name="tempo_unique_run_node")
        ]

    def __str__(self) -> str:
        return f"{self.run_id}/{self.node_key}"


class RunCheckpoint(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="checkpoints")
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=100)
    idempotency_key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "sequence"],
                name="tempo_unique_run_checkpoint_sequence",
            )
        ]

    def __str__(self) -> str:
        return f"{self.run_id}:{self.sequence} {self.kind}"


class WorkerLease(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="leases")
    worker_id = models.CharField(max_length=255, db_index=True)
    token = models.CharField(max_length=64, unique=True)
    acquired_at = models.DateTimeField()
    heartbeat_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-acquired_at"]

    def __str__(self) -> str:
        return f"{self.worker_id} → {self.run_id}"


class OperatorAction(models.Model):
    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="operator_actions")
    action = models.CharField(max_length=32)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    requested_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="tempo_operator_actions",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACCEPTED,
    )
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self) -> str:
        return f"{self.action} run {self.run_id}"


class PlatformConfigurationChange(models.Model):
    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="platform_configuration_changes"
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="configuration_changes",
        null=True,
        blank=True,
    )
    sections = models.JSONField(default=dict)
    requested_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="tempo_platform_configuration_changes",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self) -> str:
        return f"{self.project} · {self.get_status_display()}"


class WorkflowConfiguration(models.Model):
    """Live database authority for workflow, agent, model, runtime, and tool policy."""

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="workflow_configuration"
    )
    name = models.CharField(max_length=255, default="Default workflow")
    configuration = models.JSONField(
        default=dict,
        help_text=(
            "JSON object containing runtime_providers, model_providers, tool_providers, "
            "agents, and workflow. Changes are validated and loaded without a restart."
        ),
    )
    active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project"]
        verbose_name = "workflow configuration"
        verbose_name_plural = "workflow configurations"

    def clean(self) -> None:
        super().clean()
        from tempo.config import build_config
        from tempo.workflow import PLATFORM_SECTION_NAMES

        if not isinstance(self.configuration, dict):
            raise ValidationError({"configuration": "Configuration must be a JSON object."})
        unknown = set(self.configuration) - PLATFORM_SECTION_NAMES
        missing = PLATFORM_SECTION_NAMES - set(self.configuration)
        if unknown or missing:
            messages = []
            if missing:
                messages.append(f"Missing sections: {', '.join(sorted(missing))}.")
            if unknown:
                messages.append(f"Unsupported sections: {', '.join(sorted(unknown))}.")
            raise ValidationError({"configuration": " ".join(messages)})
        if not self.project_id:
            return
        latest = self.project.workflows.order_by("-version").first()
        if not latest:
            return
        raw = deepcopy(latest.config)
        raw.update(deepcopy(self.configuration))
        try:
            build_config(raw, Path(latest.path or "WORKFLOW.md"))
        except Exception as exc:
            raise ValidationError({"configuration": str(exc)}) from exc

    def __str__(self) -> str:
        return f"{self.project} · {self.name}"


class ApprovalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="approvals")
    request_key = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=100)
    title = models.CharField(max_length=500)
    details = models.JSONField(default=dict)
    proposed_arguments = models.JSONField(default=dict, blank=True)
    edited_arguments = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="tempo_approval_decisions",
        null=True,
        blank=True,
    )
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["requested_at"]

    def __str__(self) -> str:
        return f"{self.kind} for run {self.run_id}"


class ValidationAttempt(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        INVALIDATED = "invalidated", "Invalidated"

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="validations")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )
    summary = models.TextField(blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    workspace_fingerprint = models.CharField(max_length=64, blank=True)
    policy_digest = models.CharField(max_length=64, blank=True)
    required_check_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.run.issue.identifier} · {self.get_status_display()}"


class ValidationCommand(models.Model):
    validation = models.ForeignKey(
        ValidationAttempt,
        on_delete=models.CASCADE,
        related_name="commands",
    )
    position = models.PositiveIntegerField()
    check_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255)
    command = models.TextField()
    exit_code = models.IntegerField(null=True, blank=True)
    output = models.TextField(blank=True)
    cleanup = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["validation", "position"],
                name="tempo_unique_validation_command_position",
            )
        ]

    def __str__(self) -> str:
        return f"{self.position}. {self.name}"


class ProductBrief(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="product_briefs")
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    request_key = models.UUIDField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class BriefRevision(models.Model):
    brief = models.ForeignKey(ProductBrief, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    specification = models.JSONField()
    digest = models.CharField(max_length=64)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["brief", "number"],
                                               name="tempo_unique_brief_revision")]


class ExecutionPlan(models.Model):
    brief_revision = models.ForeignKey(
        BriefRevision, on_delete=models.PROTECT, related_name="plans",
    )
    number = models.PositiveIntegerField()
    specification = models.JSONField()
    digest = models.CharField(max_length=64)
    generator = models.CharField(max_length=32)
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.PROTECT, related_name="created_plans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="approved_plans")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["brief_revision", "number"],
                                               name="tempo_unique_plan_revision")]


class BuildArtifact(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.PROTECT, related_name="build_artifacts")
    digest = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    data = models.BinaryField(editable=False)
    manifest = models.JSONField()
    manifest_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["run", "manifest_digest"], name="tempo_unique_run_build_manifest",
        )]

    def __str__(self):
        return f"Build for run {self.run_id}: {self.digest[:12]}"


class PreviewDeployment(models.Model):
    artifact = models.OneToOneField(
        BuildArtifact, on_delete=models.PROTECT, related_name="preview",
    )
    token = models.UUIDField(unique=True)
    active = models.BooleanField(default=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
