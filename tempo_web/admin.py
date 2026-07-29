from django.contrib import admin

from .models import (
    AgentRun,
    AgentSession,
    ApprovalRequest,
    CredentialReference,
    Environment,
    OperatorAction,
    Organization,
    Project,
    Repository,
    RunCheckpoint,
    TrackedIssue,
    ValidationAttempt,
    ValidationCommand,
    WorkerLease,
    WorkflowVersion,
)

admin.site.register(Organization)
admin.site.register(Project)
admin.site.register(Repository)
admin.site.register(Environment)
admin.site.register(WorkflowVersion)
admin.site.register(CredentialReference)


class ReadOnlyRuntimeAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AgentRunInline(admin.TabularInline):
    model = AgentRun
    extra = 0
    fields = ("started_at", "status", "phase", "attempt", "total_tokens")
    readonly_fields = fields
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TrackedIssue)
class TrackedIssueAdmin(ReadOnlyRuntimeAdmin):
    list_display = ("identifier", "title", "state", "tracker_kind", "last_synced_at")
    list_filter = ("tracker_kind", "state", "labels")
    search_fields = ("identifier", "title", "description", "external_id")
    readonly_fields = (
        "tracker_kind",
        "external_id",
        "identifier",
        "title",
        "description",
        "state",
        "url",
        "labels",
        "native_ref",
        "first_seen_at",
        "last_synced_at",
    )
    inlines = (AgentRunInline,)


class AgentSessionInline(admin.StackedInline):
    model = AgentSession
    extra = 0
    readonly_fields = (
        "session_id",
        "thread_id",
        "turn_id",
        "process_id",
        "turn_count",
        "last_event",
        "last_event_at",
        "last_message",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ValidationAttemptInline(admin.TabularInline):
    model = ValidationAttempt
    extra = 0
    fields = ("status", "summary", "started_at", "finished_at")
    readonly_fields = fields
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AgentRun)
class AgentRunAdmin(ReadOnlyRuntimeAdmin):
    list_display = (
        "id",
        "issue",
        "status",
        "phase",
        "attempt",
        "started_at",
        "finished_at",
        "total_tokens",
    )
    list_filter = ("status", "phase", "started_at")
    search_fields = ("issue__identifier", "issue__title", "error", "pull_request_url")
    readonly_fields = (
        "issue",
        "attempt",
        "phase",
        "status",
        "workspace_path",
        "started_at",
        "finished_at",
        "error",
        "pull_request_url",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    )
    list_select_related = ("issue",)
    inlines = (AgentSessionInline, ValidationAttemptInline)


class ValidationCommandInline(admin.TabularInline):
    model = ValidationCommand
    extra = 0
    fields = ("position", "name", "command", "exit_code", "cleanup", "finished_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ValidationAttempt)
class ValidationAttemptAdmin(ReadOnlyRuntimeAdmin):
    list_display = ("id", "run", "status", "started_at", "finished_at")
    list_filter = ("status", "started_at")
    search_fields = ("run__issue__identifier", "summary")
    readonly_fields = (
        "run",
        "status",
        "summary",
        "started_at",
        "finished_at",
        "workspace_fingerprint",
    )
    list_select_related = ("run", "run__issue")
    inlines = (ValidationCommandInline,)


@admin.register(AgentSession)
class AgentSessionAdmin(ReadOnlyRuntimeAdmin):
    list_display = ("id", "run", "session_id", "thread_id", "turn_count", "last_event_at")
    search_fields = ("run__issue__identifier", "session_id", "thread_id", "turn_id")
    readonly_fields = (
        "run",
        "session_id",
        "thread_id",
        "turn_id",
        "process_id",
        "turn_count",
        "last_event",
        "last_event_at",
        "last_message",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    )
    list_select_related = ("run", "run__issue")


@admin.register(ValidationCommand)
class ValidationCommandAdmin(ReadOnlyRuntimeAdmin):
    list_display = ("id", "validation", "position", "name", "exit_code", "cleanup")
    list_filter = ("cleanup", "exit_code")
    search_fields = ("validation__run__issue__identifier", "name", "command", "output")
    readonly_fields = (
        "validation",
        "position",
        "name",
        "command",
        "exit_code",
        "output",
        "cleanup",
        "started_at",
        "finished_at",
    )
    list_select_related = ("validation", "validation__run", "validation__run__issue")


admin.site.register(RunCheckpoint, ReadOnlyRuntimeAdmin)
admin.site.register(WorkerLease, ReadOnlyRuntimeAdmin)
admin.site.register(OperatorAction, ReadOnlyRuntimeAdmin)
admin.site.register(ApprovalRequest, ReadOnlyRuntimeAdmin)


admin.site.site_header = "Tempo administration"
admin.site.site_title = "Tempo admin"
admin.site.index_title = "Persistent runtime records"
