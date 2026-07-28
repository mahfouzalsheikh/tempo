from __future__ import annotations

from django.db import models


class TrackedIssue(models.Model):
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
                fields=["tracker_kind", "external_id"],
                name="tempo_unique_tracker_issue",
            )
        ]

    def __str__(self) -> str:
        return f"{self.identifier}: {self.title}"


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    issue = models.ForeignKey(TrackedIssue, on_delete=models.PROTECT, related_name="runs")
    attempt = models.PositiveIntegerField(null=True, blank=True)
    phase = models.CharField(max_length=100, default="PreparingWorkspace")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )
    workspace_path = models.TextField(blank=True)
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
