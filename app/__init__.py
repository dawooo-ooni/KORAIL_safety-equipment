"""KORAIL 안전장비/보호구 자동 관리 대장 — Flask 애플리케이션 팩토리."""
import os
from datetime import date, datetime

from dotenv import load_dotenv

# .env 는 config 모듈을 읽기 전에 불러와야 클래스 속성에 반영된다
load_dotenv()

# 서버 시간대 (Vercel 등 UTC 서버에서도 '오늘'·기록 시각을 한국 시간으로)
if os.environ.get("APP_TZ"):
    import time as _time
    os.environ["TZ"] = os.environ["APP_TZ"]
    if hasattr(_time, "tzset"):
        _time.tzset()

from flask import Flask, flash, redirect, render_template, request, url_for  # noqa: E402
from flask_login import current_user  # noqa: E402
from flask_wtf.csrf import CSRFError  # noqa: E402

from .config import config_by_name  # noqa: E402
from .extensions import csrf, db, login_manager, migrate  # noqa: E402


def create_app(config_name=None, overrides=None):
    if config_name is None:
        config_name = os.environ.get("APP_CONFIG") or (
            "development" if os.environ.get("FLASK_ENV") == "development" else "production")
    # Vercel 은 /tmp 만 쓸 수 있다 (DB·파일은 Supabase 에 저장하므로 임시 용도로만 사용)
    instance_path = os.environ.get("INSTANCE_PATH") or ("/tmp/instance" if os.environ.get("VERCEL") else None)
    app = Flask(__name__, instance_relative_config=True, instance_path=instance_path)
    app.config.from_object(config_by_name[config_name])
    if overrides:
        app.config.update(overrides)

    for sub in ("", "uploads/photos", "backups", "tmp"):
        try:
            os.makedirs(os.path.join(app.instance_path, sub), exist_ok=True)
        except OSError:
            pass

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "로그인이 필요합니다."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "basic"

    from .models import User

    @login_manager.user_loader
    def load_user(uid):
        u = db.session.get(User, int(uid))
        return u if (u and u.status == "active") else None

    from .admin import bp as admin_bp
    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .check import bp as check_bp
    from .equipment import bp as equipment_bp
    from .labels import bp as labels_bp
    from .main import bp as main_bp
    from .notify import bp as notify_bp

    for bp in (auth_bp, main_bp, equipment_bp, check_bp, notify_bp, labels_bp, admin_bp, api_bp):
        app.register_blueprint(bp)
    csrf.exempt(api_bp)  # JSON API 는 GET 전용 (읽기)

    _register_template_helpers(app)
    _register_hooks(app)
    _register_errors(app)

    from .cli import register_cli
    register_cli(app)
    return app


def _register_template_helpers(app):
    from .services import settings as cfg
    from .services.calc import GRADES, dd_text
    from .services.ledger import icon_for

    @app.template_filter("d")
    def fmt_date(v):
        if not v:
            return ""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M")
        if isinstance(v, date):
            return v.isoformat()
        return str(v)

    @app.template_filter("dd")
    def fmt_dd(v):
        return dd_text(v)

    @app.context_processor
    def inject():
        ctx = {"GRADES": GRADES, "today": date.today(), "icon_for": icon_for}
        try:
            ctx["urgent_days"], ctx["warn_days"] = cfg.thresholds()
            ctx["logo_on"] = bool(cfg.get("logo_path"))
            ctx["allow_signup"] = cfg.get_bool("allow_signup")
        except Exception:  # noqa: BLE001 - DB 가 아직 없을 때
            ctx.update(urgent_days=7, warn_days=30, logo_on=False, allow_signup=True)
        ctx["pending_count"] = 0
        if current_user.is_authenticated and current_user.is_admin:
            from .models import User
            ctx["pending_count"] = User.query.filter_by(status="pending").count()
        return ctx


def _register_hooks(app):
    started = {"v": False}

    @app.before_request
    def hooks():
        # 스케줄러: 요청을 실제로 처리하는 프로세스에서만 한 번 시작 (reloader 중복 방지)
        if (app.config["SCHEDULER_ENABLED"] and not app.testing and not app.config["ON_VERCEL"]
                and not started["v"]):
            started["v"] = True
            from .services import scheduler
            scheduler.start(app)
        if current_user.is_authenticated:
            # 초기 비밀번호면 비밀번호 변경 강제
            if current_user.must_change_pw and request.endpoint not in (
                    "auth.change_password", "auth.logout", "static", "main.logo"):
                return redirect(url_for("auth.change_password"))

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


def _register_errors(app):
    @app.errorhandler(403)
    def e403(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def e413(e):
        mb = app.config["UPLOAD_MAX_MB"]
        flash(f"파일이 너무 큽니다. {mb}MB 이하로 올려 주세요.", "error")
        return redirect(request.referrer or url_for("main.dashboard"))

    @app.errorhandler(500)
    def e500(e):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(CSRFError)
    def ecsrf(e):
        flash("화면이 오래되어 요청이 거부되었습니다. 새로고침 후 다시 시도하세요.", "error")
        return redirect(request.referrer or url_for("main.dashboard"))
