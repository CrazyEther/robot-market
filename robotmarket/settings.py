"""Runtime settings for the robot market platform."""
import os
from pathlib import Path

import dj_database_url


BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DEBUG", "0" if os.getenv("DATABASE_URL") or os.getenv("DB_HOST") else "1") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-before-publishing")
if not DEBUG and SECRET_KEY == "dev-only-change-before-publishing":
    raise RuntimeError("DJANGO_SECRET_KEY is required when DEBUG=0")

ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "demo",
    "projects",
    "catalog",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "robotmarket.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "robotmarket.wsgi.application"
if os.getenv("DB_HOST"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["DB_HOST"],
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "CONN_MAX_AGE": 60,
    }}
else:
    DATABASES = {"default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'local.sqlite3'}", conn_max_age=60)}
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
if not DEBUG:
    STORAGES = {"staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CATALOG_PAGE_SIZE = int(os.getenv("CATALOG_PAGE_SIZE", "24"))
CATALOG_SOURCE_SHA256 = os.getenv("CATALOG_SOURCE_SHA256", "").lower()
RESEARCH_CLAIMS_SHA256 = os.getenv("RESEARCH_CLAIMS_SHA256", "").lower()
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/projects/"
LOGOUT_REDIRECT_URL = "/"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if os.getenv("TRUST_PROXY_SSL_HEADER") == "1" else None
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Only enable forced HTTPS behind a correctly configured TLS-terminating proxy.
# Local Docker/LiveServer HTTP smoke checks remain accessible without a proxy.
SECURE_SSL_REDIRECT = not DEBUG and os.getenv("ENFORCE_HTTPS", "0") == "1"
SECURE_HSTS_SECONDS = 3600 if SECURE_SSL_REDIRECT else 0
