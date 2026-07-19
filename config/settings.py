from pathlib import Path
from decouple import config, Csv
import os
from django.utils.timezone import get_default_timezone

import mimetypes

mimetypes.add_type("text/css", ".css", True)
mimetypes.add_type("application/javascript", ".js", True)

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="dev-insecure-key-a-changer")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="127.0.0.1,localhost", cast=Csv())

# --- Applications ---
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Bibliotheques tierces
    "rest_framework",
    # Applications metier
    "core",
    "dashboard",
    "clients",
    "catalogue",
    "stock",
    "ventes",
    "caisse",
    "api",
    "django.contrib.humanize",
]

# --- Middleware ---
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --- URLS ---
ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- WSGI ---
WSGI_APPLICATION = "config.wsgi.application"

# --- Base de donnees : bascule sqlite (dev) / mysql (production) ---
DATABASES = {
    "default": {
        "ENGINE": config("DB_ENGINE", default="django.db.backends.mysql"),
        "NAME": config("DB_NAME", default="medstock"),
        "USER": config("DB_USER", default="mde_kalaye"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="3306"),
    }
}

# --- Authentification ---
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# --- Messages : mappe les tags Django sur les classes d'alerte Bootstrap 5 ---
from django.contrib.messages import constants as messages_constants  # noqa: E402

MESSAGE_TAGS = {
    messages_constants.DEBUG: "secondary",
    messages_constants.INFO: "info",
    messages_constants.SUCCESS: "success",
    messages_constants.WARNING: "warning",
    messages_constants.ERROR: "danger",
}

# --- Django REST Framework (API stock temps reel) ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
}

LANGUAGE_CODE = config("LANGUAGE_CODE", default="fr-fr")
# Africa/Dakar = UTC+0 toute l'annee (pas d'heure d'ete) : utiliser "UTC" ici
# est strictement equivalent en heure locale, mais evite a Django d'emettre
# des CONVERT_TZ(...) MySQL — inutilisables sur ce serveur car les tables de
# fuseaux horaires (mysql.time_zone_name) n'y sont pas chargees, ce qui
# faisait silencieusement echouer tout filtre par date (__date, __gte...).
TIME_ZONE = "UTC"
USE_I18N = True
USE_L10N = True
USE_TZ = True


STATIC_URL = "/static/"
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

