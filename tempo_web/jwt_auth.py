from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse


def issue_access_token(user: Any) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.JWT_ACCESS_TTL_SECONDS)
    token = jwt.encode(
        {
            "sub": str(user.pk),
            "username": user.get_username(),
            "type": "access",
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "iat": now,
            "exp": expires_at,
            "jti": uuid.uuid4().hex,
        },
        _signing_key(),
        algorithm="HS256",
    )
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        _signing_key(),
        algorithms=["HS256"],
        audience=settings.JWT_AUDIENCE,
        issuer=settings.JWT_ISSUER,
        options={"require": ["sub", "type", "iss", "aud", "iat", "exp", "jti"]},
    )
    if claims.get("type") != "access":
        raise jwt.InvalidTokenError("token is not an access token")
    return claims


def user_for_access_token(token: str) -> Any | None:
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
    except (jwt.InvalidTokenError, TypeError, ValueError):
        return None
    return get_user_model().objects.filter(pk=user_id, is_active=True).first()


def bearer_token(request: HttpRequest) -> str | None:
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _signing_key() -> bytes:
    return hashlib.sha256(f"tempo-jwt:{settings.SECRET_KEY}".encode()).digest()


def set_access_cookie(response: HttpResponse, token: str, expires_at: datetime) -> None:
    response.set_cookie(
        settings.JWT_COOKIE_NAME,
        token,
        expires=expires_at,
        httponly=True,
        secure=settings.JWT_COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )


def clear_access_cookie(response: HttpResponse) -> None:
    response.delete_cookie(
        settings.JWT_COOKIE_NAME,
        path="/",
        samesite="Lax",
    )


class JWTAuthenticationMiddleware:
    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.tempo_jwt_authenticated = False
        user = getattr(request, "_tempo_bearer_user", None)
        if user is not None:
            request.user = user
            request.tempo_jwt_authenticated = True
        elif not request.user.is_authenticated:
            token = request.COOKIES.get(settings.JWT_COOKIE_NAME, "")
            user = user_for_access_token(token) if token else None
            if user is not None:
                request.user = user
                request.tempo_jwt_authenticated = True
        return self.get_response(request)


class JWTBearerCSRFMiddleware:
    """Exempt only cryptographically valid Bearer requests from browser CSRF checks."""

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        token = bearer_token(request) if request.path.startswith("/api/") else None
        user = user_for_access_token(token) if token else None
        if user is not None:
            request._tempo_bearer_user = user
            request._dont_enforce_csrf_checks = True
        return self.get_response(request)
