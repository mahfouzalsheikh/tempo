import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "tempo-local-only")
JWT_ISSUER = "tempo"
JWT_AUDIENCE = "tempo-operators"
JWT_ACCESS_TTL_SECONDS = int(os.getenv("TEMPO_JWT_ACCESS_TTL_SECONDS", "28800"))
if JWT_ACCESS_TTL_SECONDS < 60:
    raise RuntimeError("TEMPO_JWT_ACCESS_TTL_SECONDS must be at least 60")
JWT_COOKIE_NAME = "tempo_access"
JWT_COOKIE_SECURE = os.getenv("TEMPO_JWT_COOKIE_SECURE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tempo_web.urls"
ASGI_APPLICATION = "tempo_web.asgi.application"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    parsed_database_url = urlparse(DATABASE_URL)
    if parsed_database_url.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("DATABASE_URL must use postgres:// or postgresql://")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed_database_url.path.lstrip("/")),
            "USER": unquote(parsed_database_url.username or ""),
            "PASSWORD": unquote(parsed_database_url.password or ""),
            "HOST": parsed_database_url.hostname or "",
            "PORT": parsed_database_url.port or 5432,
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 10},
        }
    }
else:
    DATABASE_PATH = Path(os.getenv("TEMPO_DATABASE_PATH", BASE_DIR / "var" / "tempo.sqlite3"))
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DATABASE_PATH,
            "OPTIONS": {"timeout": 30},
        }
    }
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "tempo_web.jwt_auth.JWTBearerCSRFMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tempo_web.jwt_auth.JWTAuthenticationMiddleware",
    "tempo_web.access.OperatorAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "tempo_web.apps.TempoWebConfig",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PACKAGE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
STATIC_URL = "static/"
USE_TZ = True
TIME_ZONE = os.getenv("TEMPO_TIME_ZONE", "UTC")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
