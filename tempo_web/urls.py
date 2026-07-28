from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("admin/", views.admin_runtime, name="admin_runtime"),
    path("admin/configuration/", views.admin_configuration, name="admin_configuration"),
    path("static/<path:name>", views.static_asset, name="static_asset"),
    path("healthz", views.health, name="health"),
    path("api/v1/state", views.state, name="state"),
    path("api/v1/admin", views.admin_state, name="admin_state"),
    path("api/v1/refresh", views.refresh, name="refresh"),
    path("api/v1/<str:identifier>", views.issue, name="issue"),
]
