import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "tempo-local-only")
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tempo_web.urls"
ASGI_APPLICATION = "tempo_web.asgi.application"
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
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
