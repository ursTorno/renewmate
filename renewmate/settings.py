# Django settings for the RenewMate project.
# RenewMate is a personal document and renewal tracking application.
# This file configures the whole project: installed apps, the SQLite
# database, templates, static files, media uploads and login redirects.

import os
import sys
from pathlib import Path

from django.contrib.messages import constants as message_constants

# BASE_DIR points to the project root (the folder that contains manage.py).
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# The secret key is read from the environment when available so it is never
# committed to version control. The fallback key is for local development
# only - set DJANGO_SECRET_KEY before deploying this project.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-before-production",
)

# Debug mode is ON for local development. Set DJANGO_DEBUG=False in production.
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

# Comma separated hosts. Defaults to the local development hosts; set
# DJANGO_ALLOWED_HOSTS in production (e.g. "renewmate.example.com").
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
    .split(",")
    if host.strip()
]

# Render terminates HTTPS at its proxy and forwards the original scheme in
# this header. Trust it so Django can validate HTTPS requests and CSRF origins.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    "https://{}".format(host)
    for host in ALLOWED_HOSTS
    if host not in {"127.0.0.1", "localhost"}
]

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "cloudinary",
    "cloudinary_storage",
    # RenewMate apps
    "accounts",
    "documents",
    "dashboard",
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

ROOT_URLCONF = "renewmate.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "renewmate.wsgi.application"

# ---------------------------------------------------------------------------
# Database - SQLite for development (no extra setup needed)
# ---------------------------------------------------------------------------
import dj_database_url

DATABASES = {
    "default": dj_database_url.config(
        default="sqlite:///{}".format(BASE_DIR / "db.sqlite3"),
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation - expiry dates are calculated for this time zone
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files (CSS / JS) and user uploads (media)
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files go to Cloudinary when credentials are configured (the
# deployed setup) and to MEDIA_ROOT on the local disk otherwise. Without this
# switch a local run crashes on every document file URL, because the Cloudinary
# storage refuses to load without CLOUDINARY_URL.
CLOUDINARY_CONFIGURED = bool(
    os.environ.get("CLOUDINARY_URL") or os.environ.get("CLOUDINARY_CLOUD_NAME")
)

# The hashed-manifest static storage needs the staticfiles.json that
# `collectstatic` writes. Local development (and the test suite) never runs
# collectstatic, so those runs use plain static storage instead.
RUNNING_TESTS = "test" in sys.argv
STATICFILES_BACKEND = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
    if not DEBUG and not RUNNING_TESTS
    else "django.contrib.staticfiles.storage.StaticFilesStorage"
)

STORAGES = {
    "default": {
        "BACKEND": (
            "cloudinary_storage.storage.MediaCloudinaryStorage"
            if CLOUDINARY_CONFIGURED
            else "django.core.files.storage.FileSystemStorage"
        )
    },
    "staticfiles": {"BACKEND": STATICFILES_BACKEND},
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Public base URL (used in the reminder emails)
# ---------------------------------------------------------------------------
# Only used for the "Log in to RenewMate" link inside reminder emails. The
# default is the local development / demo address; set RENEWMATE_BASE_URL
# in production (e.g. "https://renewmate.example.com").
BASE_URL = os.environ.get("RENEWMATE_BASE_URL", "http://127.0.0.1:8000")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Authentication redirects
# ---------------------------------------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

# ---------------------------------------------------------------------------
# Upload rules (used by the document form validators)
# ---------------------------------------------------------------------------
MAX_UPLOAD_SIZE_MB = 5

# Map Django message levels to Bootstrap alert classes ("error" -> "danger").
MESSAGE_TAGS = {message_constants.ERROR: "danger"}

# ---------------------------------------------------------------------------
# Email (used by: python manage.py send_email_reminders)
# ---------------------------------------------------------------------------
# In development, emails are printed to the terminal instead of being sent.
# Set the EMAIL_* environment variables to switch to a real SMTP server.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() in ("true", "1", "yes")
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "RenewMate <noreply@renewmate.local>")
