from django.contrib import admin
from django.urls import path

from . import intake_views, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_page, name="login"),
    path("logout/", views.logout_page, name="logout"),
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
    path("api/v1/auth/login", views.auth_login, name="auth_login"),
    path("api/v1/auth/logout", views.auth_logout, name="auth_logout"),
    path("api/v1/auth/me", views.auth_me, name="auth_me"),
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
    path("ideas/", intake_views.ideas, name="ideas"),
    path("ideas/new/", intake_views.edit_brief, name="idea_new"),
    path("ideas/<int:brief_id>/", intake_views.idea_detail, name="idea_detail"),
    path("ideas/<int:brief_id>/edit/", intake_views.edit_brief, name="idea_edit"),
    path("ideas/<int:brief_id>/plan/", intake_views.edit_plan, name="idea_plan"),
    path("ideas/<int:brief_id>/approve/", intake_views.approve, name="idea_approve"),
    path("api/v1/briefs", intake_views.api, name="briefs_api"),
    path("api/v1/briefs/<int:brief_id>", intake_views.api, name="brief_api"),
    path("api/v1/briefs/<int:brief_id>/<str:action>", intake_views.api, name="brief_action_api"),
    path("api/v1/<str:identifier>", views.issue, name="issue"),
]
