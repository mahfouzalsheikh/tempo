from django.contrib import admin
from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("ops/", views.admin_runtime, name="admin_runtime"),
    path("ops/configuration/", views.admin_configuration, name="admin_configuration"),
    path("static/<path:name>", views.static_asset, name="static_asset"),
    path("healthz", views.health, name="health"),
    path("api/v1/state", views.state, name="state"),
    path("api/v1/events", views.state_events, name="state_events"),
    path("api/v1/admin", views.admin_state, name="admin_state"),
    path("api/v1/refresh", views.refresh, name="refresh"),
    path("api/v1/approvals", views.approvals, name="approvals"),
    path("api/v1/control", views.control_state, name="control_state"),
    path("api/v1/platform", views.platform_configuration, name="platform_configuration"),
    path(
        "api/v1/platform/<slug:organization>/<slug:project>",
        views.update_platform_configuration,
        name="update_platform_configuration",
    ),
    path(
        "api/v1/approvals/<int:approval_id>/decision",
        views.approval_decision,
        name="approval_decision",
    ),
    path(
        "api/v1/runs/<int:run_id>/<str:action>",
        views.run_action,
        name="run_action",
    ),
    path("api/v1/<str:identifier>", views.issue, name="issue"),
]
