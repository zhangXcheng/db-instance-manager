"""
Django 配置。

设计原则：所有可变配置走环境变量，同一份代码可在本地 / CI / 生产运行，
避免「本地能跑线上炸」。敏感项（密钥、数据库口令）不写死。
"""
import os
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# 可选加载 .env，方便本地开发；生产一般由容器/K8s 注入环境变量
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(key, default).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
DEBUG = _env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-key-do-not-use-in-prod")
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "*") or ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 三方
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    # 业务
    "inventory",
]

MIDDLEWARE = [
    # 放在最外层：请求进来第一个执行、响应出去最后一个执行，
    # 这样统计到的耗时才是包含全部中间件与视图的「端到端」耗时。
    "core.middleware.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
if os.getenv("DB_ENGINE", "sqlite").lower() == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "db_instance_manager"),
            "USER": os.getenv("DB_USER", "root"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            # 复用连接降低握手开销；健康检查避免拿到被中间件杀掉的死连接
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": 20},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# 国际化 / 时区
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    # 端口探测接口容易被当成内网扫描器滥用，按用户/IP 限速；
    # 登录接口是暴力破解的首选目标，同样要限速。
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "probe": "60/min",
        "login": "10/min",
    },
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# beat 用本地时区解释 crontab，否则「每天 00:00」会变成 UTC 00:00（北京 08:00）
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # 轮换是重 IO 任务，避免单 worker 囤积
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540

# 注意：这里必须用 crontab 而不是「秒数」。
# 写 `24 * 60 * 60` 的语义是「距 beat 上次触发 24 小时」，
# beat 在 14:37 启动，统计就会永远在 14:37 跑，与「每天 00:00」不符；
# 而且进程重启一次，执行时间就整体漂移一次。
CELERY_BEAT_SCHEDULE = {
    # 每 12 小时整点（00:00 / 12:00）轮换一次密码
    "rotate-instance-passwords": {
        "task": "inventory.tasks.rotate_all_passwords",
        "schedule": crontab(hour="*/12", minute=0),
    },
    # 每天 00:00 汇总实例数量
    "aggregate-daily-instance-stats": {
        "task": "inventory.tasks.aggregate_daily_instance_stats",
        "schedule": crontab(hour=0, minute=0),
    },
}

# ---------------------------------------------------------------------------
# 业务参数
# ---------------------------------------------------------------------------
# 加密密钥列表：首个用于加密，全部用于解密，支持平滑轮换密钥。
FERNET_SECRETS = _env_list("FERNET_SECRETS", "dev-only-secret-please-replace-me")

CREDENTIAL_ROTATION_INTERVAL_HOURS = int(os.getenv("CREDENTIAL_ROTATION_INTERVAL_HOURS", "12"))
CREDENTIAL_PASSWORD_LENGTH = int(os.getenv("CREDENTIAL_PASSWORD_LENGTH", "24"))
CREDENTIAL_DRIVER = os.getenv(
    "CREDENTIAL_DRIVER", "inventory.services.drivers.SimulatedDriver"
)

TCP_PROBE_TIMEOUT = float(os.getenv("TCP_PROBE_TIMEOUT", "3"))
TCP_PROBE_MAX_WORKERS = int(os.getenv("TCP_PROBE_MAX_WORKERS", "8"))

# 中间件：超过该阈值打 WARNING，便于捞慢请求
SLOW_REQUEST_THRESHOLD_MS = int(os.getenv("SLOW_REQUEST_THRESHOLD_MS", "500"))

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[{asctime}] {levelname:<7} {name}: {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "inventory": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "core": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# 生产环境自检
# ---------------------------------------------------------------------------
# 原则：宁可启动失败，也不要带着开发默认值上线。
# 这两项一旦漏配，后果分别是「会话/签名可被伪造」和「密码密文等同明文」。
if not DEBUG:
    if SECRET_KEY == "dev-insecure-key-do-not-use-in-prod":
        raise ImproperlyConfigured("生产环境必须通过 DJANGO_SECRET_KEY 设置独立密钥")

    if FERNET_SECRETS == ["dev-only-secret-please-replace-me"]:
        raise ImproperlyConfigured(
            "生产环境必须通过 FERNET_SECRETS 配置加密密钥（建议由 KMS / Vault 下发）"
        )

    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
