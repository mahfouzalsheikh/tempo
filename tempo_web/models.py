from __future__ import annotations

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

    def __str__(self) -> str:
        return self.session_id or f"Session for run {self.run_id}"


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
