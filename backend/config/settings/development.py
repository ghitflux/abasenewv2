from decouple import config

from config.runtime import enforce_mysql_only
from .base import *

DEBUG = True

JWT_ENABLE_TOKEN_BLACKLIST = config(
    "JWT_ENABLE_TOKEN_BLACKLIST",
    default=False,
    cast=bool,
)
if (
    not JWT_ENABLE_TOKEN_BLACKLIST
    and "rest_framework_simplejwt.token_blacklist" in INSTALLED_APPS
):
    INSTALLED_APPS.remove("rest_framework_simplejwt.token_blacklist")
SIMPLE_JWT["ROTATE_REFRESH_TOKENS"] = JWT_ENABLE_TOKEN_BLACKLIST
SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"] = JWT_ENABLE_TOKEN_BLACKLIST

# Em desenvolvimento com docker-compose, o worker Celery fica ativo.
# Mantenha o processamento assíncrono para evitar que uploads/retornos pesados
# travem a requisição HTTP; use CELERY_TASK_ALWAYS_EAGER=True apenas em debug
# pontual sem worker.
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = config(
    "CELERY_TASK_EAGER_PROPAGATES",
    default=CELERY_TASK_ALWAYS_EAGER,
    cast=bool,
)

enforce_mysql_only(DATABASES, "config.settings.development")
