import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "tempo-local-only")
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tempo_web.urls"
ASGI_APPLICATION = "tempo_web.asgi.application"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
INSTALLED_APPS = ["django.contrib.staticfiles"]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PACKAGE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]
STATIC_URL = "static/"
STATICFILES_DIRS = [PACKAGE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
