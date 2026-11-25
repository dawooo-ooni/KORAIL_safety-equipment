"""로그인, 로그아웃, 회원가입 신청, 신청 상태 조회, 비밀번호 변경."""
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import Office, User
from ..services import alerts, audit
from ..services import settings as cfg
from ..utils import EMAIL_RE, LOGIN_RE, clean, password_problem

bp = Blueprint("auth", __name__)

MAX_FAILS = 5
LOCK_MINUTES = 10


def _safe_next(target):
    if not target:
        return None
    u = urlparse(target)
    return target if (not u.netloc and not u.scheme and target.startswith("/")) else None


def _find(login_id):
    return User.query.filter(db.func.lower(User.login_id) == (login_id or "").lower()).first()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    error = None
    login_id = ""
    if request.method == "POST":
        login_id = clean(request.form.get("login_id"), 20)
        pw = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        u = _find(login_id)
        now = datetime.now()
        if u and u.locked_until and u.locked_until > now:
            left = int((u.locked_until - now).total_seconds() // 60) + 1
            error = f"로그인 시도가 너무 많아 잠겼습니다. 약 {left}분 후 다시 시도하세요."
        elif not u or not u.check_password(pw):
            if u:
                u.failed_logins = (u.failed_logins or 0) + 1
                if u.failed_logins >= MAX_FAILS:
                    u.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    u.failed_logins = 0
                    error = f"비밀번호를 {MAX_FAILS}회 잘못 입력해 {LOCK_MINUTES}분 동안 로그인이 잠겼습니다."
                    audit.log("login_lock", "user", u.id, None, {"login_id": u.login_id}, user_id=None)
            audit.log("login_fail", "user", u.id if u else None, None, {"login_id": login_id}, user_id=None)
            db.session.commit()
            error = error or "아이디 또는 비밀번호가 올바르지 않습니다."
        elif u.status == "pending":
            error = "가입 신청이 아직 승인되지 않았습니다. 관리자 승인 후 로그인할 수 있습니다."
        elif u.status == "rejected":
            error = ("가입 신청이 반려되었습니다." + (f" (사유: {u.reject_reason})" if u.reject_reason else "")
                     + " 내용을 고쳐 다시 신청할 수 있습니다.")
        elif u.status == "suspended":
            error = "사용이 중지된 계정입니다. 관리자에게 문의하세요."
        else:
            u.failed_logins = 0
            u.locked_until = None
            u.last_login_at = now
            db.session.commit()
            session.permanent = True
            login_user(u, remember=remember)
            flash(f"{u.name}님, 환영합니다.", "ok")
            if u.must_change_pw:
                return redirect(url_for("auth.change_password"))
            return redirect(_safe_next(request.args.get("next")) or url_for("main.dashboard"))
    show_samples = User.query.filter(User.login_id.in_(("manager01", "user01", "user02")),
                                     User.status == "active").count() > 0
    admin_first = User.query.filter(User.login_id == "admin", User.must_change_pw.is_(True),
                                    User.last_login_at.is_(None)).first() is not None
    return render_template("auth/login.html", error=error, login_id=login_id,
                           show_samples=show_samples, admin_first=admin_first)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("로그아웃했습니다.", "ok")
    return redirect(url_for("auth.login"))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    offices = Office.query.filter_by(is_active=True).order_by(Office.code).all()
    if not cfg.get_bool("allow_signup"):
        return render_template("auth/signup.html", closed=True, offices=offices, f={})
    f = request.form
    error = None
    if request.method == "POST":
        login_id = clean(f.get("login_id"), 20)
        name = clean(f.get("name"), 50)
        office_id = f.get("office_id", type=int)
        email = clean(f.get("email"), 120)
        memo = clean(f.get("memo"), 200)
        pw, pw2 = f.get("password") or "", f.get("password2") or ""
        ex = _find(login_id)
        if not LOGIN_RE.match(login_id):
            error = "아이디는 영문·숫자·._- 3~20자로 입력하세요."
        elif not name:
            error = "이름을 입력하세요."
        elif not office_id or not db.session.get(Office, office_id):
            error = "소속 사업소를 선택하세요."
        elif email and not EMAIL_RE.match(email):
            error = "이메일 형식이 올바르지 않습니다."
        elif password_problem(pw):
            error = password_problem(pw)
        elif pw != pw2:
            error = "비밀번호가 서로 다릅니다."
        elif ex and ex.status == "pending":
            error = "이미 승인 대기 중인 아이디입니다. [신청 상태 조회]에서 확인하세요."
        elif ex and ex.status != "rejected":
            error = "이미 사용 중인 아이디입니다."
        if not error:
            u = ex or User(login_id=login_id)
            u.name, u.office_id, u.email, u.signup_memo = name, office_id, email or None, memo or None
            u.role, u.status, u.reject_reason = "user", "pending", None
            u.created_at = datetime.now()
            u.set_password(pw)
            if not ex:
                db.session.add(u)
            db.session.flush()
            audit.log("signup", "user", u.id, None, u.to_audit(), user_id=None)
            db.session.commit()
            # 관리자에게 알림 메일 (선택, 실패해도 무시)
            for a in User.query.filter_by(role="admin", status="active").all():
                if a.email:
                    alerts.send_simple(a.email, f"[안전장비 관리대장] 회원가입 승인 요청: {u.name} ({u.login_id})",
                                       f"{u.name}({u.login_id})님이 회원가입을 신청했습니다.\n메모: {u.signup_memo or '-'}\n"
                                       f"승인 화면: {url_for('admin.users', _external=True)}")
            return render_template("auth/signup_done.html", login_id=u.login_id)
    return render_template("auth/signup.html", closed=False, offices=offices, f=f, error=error)


@bp.route("/signup/status")
def signup_status():
    login_id = clean(request.args.get("login_id"), 20)
    u = _find(login_id) if login_id else None
    return render_template("auth/signup_status.html", login_id=login_id, u=u, searched=bool(login_id))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    forced = current_user.must_change_pw
    error = None
    if request.method == "POST":
        old = request.form.get("old_password") or ""
        a, b = request.form.get("password") or "", request.form.get("password2") or ""
        if not forced and not current_user.check_password(old):
            error = "현재 비밀번호가 올바르지 않습니다."
        elif password_problem(a, current_user):
            error = password_problem(a, current_user)
        elif a != b:
            error = "새 비밀번호가 서로 다릅니다."
        else:
            current_user.set_password(a)
            current_user.must_change_pw = False
            audit.log("password_change", "user", current_user.id)
            db.session.commit()
            flash("비밀번호를 변경했습니다.", "ok")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/password.html", forced=forced, error=error)
