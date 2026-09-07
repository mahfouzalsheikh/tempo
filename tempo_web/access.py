"""Protect operational reads before views load snapshots or subscribe to events."""

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.utils.cache import patch_vary_headers
from django.utils.deprecation import MiddlewareMixin
from jwt import InvalidTokenError

from .jwt_auth import bearer_token, decode_access_token

PRIVATE_PAGES = {"dashboard", "admin_runtime", "admin_configuration"}
PUBLIC_API = {"auth_login", "auth_logout"}


class OperatorAccessMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        name = request.resolver_match.url_name
        api = request.path.startswith("/api/")
        if name not in PRIVATE_PAGES and not api:
            return None
        request.tempo_private_response = True
        if api and name in PUBLIC_API:
            return None
        if not request.user.is_authenticated:
            if api:
                return JsonResponse({"error": "authentication_required"}, status=401)
            return redirect_to_login(request.get_full_path(), login_url="/login/")
        if getattr(request, "tempo_jwt_authenticated", False):
            from django.conf import settings

            token = bearer_token(request) or request.COOKIES.get(settings.JWT_COOKIE_NAME)
            try:
                request.tempo_access_expires_at = decode_access_token(token)["exp"]
            except InvalidTokenError:
                if api:
                    return JsonResponse({"error": "authentication_required"}, status=401)
                return redirect_to_login(request.get_full_path(), login_url="/login/")
        return None

    def process_response(self, request, response):
        if getattr(request, "tempo_private_response", False):
            response["Cache-Control"] = "no-store, no-transform"
            patch_vary_headers(response, ("Cookie", "Authorization"))
        return response
