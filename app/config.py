"""환경설정. 값은 .env(python-dotenv)에서 읽는다."""
import os
from datetime import timedelta


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _db_url():
    url = (os.environ.get("DATABASE_URL") or "sqlite:///ledger.db").strip()
    # Supabase 등에서 받은 postgres:// · postgresql:// 주소는 psycopg(3) 드라이버로 연결
    for p in ("postgres://", "postgresql://"):
        if url.startswith(p):
            return "postgresql+psycopg://" + url[len(p):]
    return url


def _engine_options(url):
    if url.startswith("postgresql"):
        from sqlalchemy.pool import NullPool
        # 서버리스 + Supabase 연결 풀러(트랜잭션 모드): 연결을 재사용하지 않고, prepared statement 끔
        return {"poolclass": NullPool, "connect_args": {"prepare_threshold": None}}
    return {}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-only-secret-change-me"
    # 상대 경로 sqlite:///ledger.db 는 Flask-SQLAlchemy가 instance/ 폴더 기준으로 해석한다.
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(SQLALCHEMY_DATABASE_URI)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 업로드 파일 저장소: 둘 다 설정되면 Supabase Storage(비공개 버킷), 아니면 instance/uploads
    SUPABASE_URL = os.environ.get("SUPABASE_URL") or ""
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET") or "uploads"
    # Vercel Cron 이 /cron/daily 를 호출할 때 쓰는 비밀값 (Vercel 이 Authorization 헤더로 보냄)
    CRON_SECRET = os.environ.get("CRON_SECRET") or ""
    ON_VERCEL = bool(os.environ.get("VERCEL"))

    APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    ADMIN_INIT_PASSWORD = os.environ.get("ADMIN_INIT_PASSWORD") or "ChangeMe!2026"

    PERMANENT_SESSION_LIFETIME = timedelta(minutes=_int("SESSION_LIFETIME_MIN", 480))
    REMEMBER_COOKIE_DURATION = timedelta(days=_int("REMEMBER_DAYS", 14))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)  # HTTPS 운영 시 1
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)

    WTF_CSRF_TIME_LIMIT = None  # 세션 동안 유효

    MAIL_BACKEND = (os.environ.get("MAIL_BACKEND") or "console").strip().lower()
    SMTP_HOST = os.environ.get("SMTP_HOST") or ""
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USER = os.environ.get("SMTP_USER") or ""
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or ""
    SMTP_USE_TLS = _bool("SMTP_USE_TLS", True)
    MAIL_FROM = os.environ.get("MAIL_FROM") or "safety-ledger@example.com"

    SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", False)
    UPLOAD_MAX_MB = _int("UPLOAD_MAX_MB", 10)
    MAX_CONTENT_LENGTH = UPLOAD_MAX_MB * 1024 * 1024

    # CSS·JS·그림을 브라우저가 하루 동안 캐시 (CSS·JS 링크에는 배포 버전 ?v= 를 붙여 배포 즉시 갱신)
    SEND_FILE_MAX_AGE_DEFAULT = 86400
    STATIC_VERSION = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or os.environ.get("VERCEL_DEPLOYMENT_ID") or "")[:12]

    PER_PAGE = 50
    BACKUP_KEEP_DAYS = 30
    TESTING = False
    DEBUG = False


class DevConfig(Config):
    DEBUG = True
    SEND_FILE_MAX_AGE_DEFAULT = None  # 개발 중에는 캐시하지 않음


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_ENGINE_OPTIONS = {}
    SUPABASE_URL = ""
    SUPABASE_SERVICE_KEY = ""
    CRON_SECRET = "test-cron"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret"
    MAIL_BACKEND = "console"
    SCHEDULER_ENABLED = False
    ADMIN_INIT_PASSWORD = "Admin!2026"
    APP_BASE_URL = "http://testserver"


config_by_name = {
    "development": DevConfig,
    "production": Config,
    "testing": TestConfig,
}
