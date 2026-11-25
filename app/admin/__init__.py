"""관리자: 사용자 승인·관리, 기준표, 사업소, 설정, 가져오기/내보내기, 백업, 감사 로그."""
import io
import json
import os
import secrets
import string
import uuid
from datetime import date, datetime

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request, send_file, session,
                   url_for)
from flask_login import current_user
from PIL import Image, UnidentifiedImageError

from ..extensions import db
from ..models import AuditLog, Equipment, EquipmentType, Office, User, WorkCheck
from ..services import alerts, audit, io_excel
from ..services import settings as cfg
from ..services.backup import (BackupError, backup_db, export_json, import_legacy, list_db_backups,
                               restore_json, APP_ID)
from ..services.ledger import ICON_BY_CODE, lookups
from ..services.seed import SAMPLE_LOGINS, delete_samples, detach_user
from ..services.storage import StorageError, get_store
from ..models import Setting
from ..utils import CODE_RE, EMAIL_RE, LOGIN_RE, TIME_RE, admin_required, clean, password_problem

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _temp_password(n=10):
    # 손으로 옮겨 적을 때 헷갈리는 글자(1 l I O 0 o) 제외
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "1lIO0o")
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


def _active_admins():
    return User.query.filter_by(role="admin", status="active").count()


# ======================= 사용자 =======================

@bp.route("/users")
@admin_required
def users():
    pending = User.query.filter_by(status="pending").order_by(User.created_at).all()
    others = (User.query.filter(User.status != "pending")
              .order_by(User.role.desc(), User.status, User.login_id).all())
    offices, _ = lookups()
    return render_template("admin/users.html", pending=pending, users=others, offices=offices,
                           sample_logins=SAMPLE_LOGINS, now=datetime.now())


def _user_or_404(uid):
    return db.get_or_404(User, uid)


@bp.route("/users/<int:uid>/approve", methods=["POST"])
@admin_required
def approve(uid):
    u = _user_or_404(uid)
    if u.status not in ("pending", "rejected"):
        flash("승인 대기 상태가 아닙니다.", "error")
        return redirect(url_for("admin.users"))
    role = request.form.get("role") if request.form.get("role") in ("user", "admin") else "user"
    before = u.to_audit()
    u.status, u.role, u.reject_reason = "active", role, None
    u.approved_by, u.approved_at = current_user.id, datetime.now()
    audit.log("approve", "user", u.id, before, u.to_audit())
    db.session.commit()
    if u.email:
        alerts.send_simple(u.email, "[안전장비 관리대장] 회원가입이 승인되었습니다",
                           f"{u.name}님, 회원가입 신청이 {'관리자' if role == 'admin' else '사용자'} 권한으로 승인되었습니다.\n"
                           f"로그인: {url_for('auth.login', _external=True)}")
    flash(f"{u.name}님의 가입을 {'관리자' if role == 'admin' else '사용자'} 권한으로 승인했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/reject", methods=["POST"])
@admin_required
def reject(uid):
    u = _user_or_404(uid)
    if u.status != "pending":
        flash("승인 대기 상태가 아닙니다.", "error")
        return redirect(url_for("admin.users"))
    before = u.to_audit()
    u.status = "rejected"
    u.reject_reason = clean(request.form.get("reason"), 200) or None
    audit.log("reject", "user", u.id, before, {**u.to_audit(), "reject_reason": u.reject_reason})
    db.session.commit()
    if u.email:
        alerts.send_simple(u.email, "[안전장비 관리대장] 회원가입 신청이 반려되었습니다",
                           f"{u.name}님, 회원가입 신청이 반려되었습니다.\n사유: {u.reject_reason or '-'}\n"
                           f"내용을 고쳐 다시 신청할 수 있습니다: {url_for('auth.signup', _external=True)}")
    flash("가입 신청을 반려했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/suspend", methods=["POST"])
@admin_required
def suspend(uid):
    u = _user_or_404(uid)
    if u.id == current_user.id:
        flash("본인 계정은 중지할 수 없습니다.", "error")
    elif u.is_admin and _active_admins() <= 1:
        flash("마지막 관리자는 중지할 수 없습니다.", "error")
    else:
        before = u.to_audit()
        u.status = "suspended"
        audit.log("suspend", "user", u.id, before, u.to_audit())
        db.session.commit()
        flash(f"{u.name}님의 계정 사용을 중지했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/activate", methods=["POST"])
@admin_required
def activate(uid):
    u = _user_or_404(uid)
    before = u.to_audit()
    u.status = "active"
    u.failed_logins, u.locked_until = 0, None
    audit.log("activate", "user", u.id, before, u.to_audit())
    db.session.commit()
    flash(f"{u.name}님의 계정을 다시 사용하도록 했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/reset-password", methods=["POST"])
@admin_required
def reset_password(uid):
    u = _user_or_404(uid)
    pw = _temp_password()
    u.set_password(pw)
    u.must_change_pw = True
    u.failed_logins, u.locked_until = 0, None
    audit.log("reset_password", "user", u.id, None, {"login_id": u.login_id})
    db.session.commit()
    # 임시 비밀번호는 이번 한 번만 표시 (flash 는 한 번 표시 후 사라짐)
    flash(f"{u.name}({u.login_id})님의 임시 비밀번호: {pw}  — 이 화면을 벗어나면 다시 볼 수 없습니다. "
          f"다음 로그인 때 본인이 새 비밀번호로 바꾸게 됩니다.", "temp")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/role", methods=["POST"])
@admin_required
def role(uid):
    u = _user_or_404(uid)
    new_role = request.form.get("role")
    if new_role not in ("user", "admin"):
        flash("권한 값이 올바르지 않습니다.", "error")
    elif u.is_admin and new_role != "admin" and _active_admins() <= 1:
        flash("마지막 관리자는 강등할 수 없습니다.", "error")
    else:
        before = u.to_audit()
        u.role = new_role
        audit.log("role", "user", u.id, before, u.to_audit())
        db.session.commit()
        flash(f"{u.name}님의 권한을 {'관리자' if new_role == 'admin' else '사용자'}로 바꿨습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/edit", methods=["POST"])
@admin_required
def user_edit(uid):
    u = _user_or_404(uid)
    name = clean(request.form.get("name"), 50)
    email = clean(request.form.get("email"), 120)
    office_id = request.form.get("office_id", type=int)
    if not name:
        flash("이름을 입력하세요.", "error")
    elif email and not EMAIL_RE.match(email):
        flash("이메일 형식이 올바르지 않습니다.", "error")
    else:
        before = u.to_audit()
        u.name, u.email = name, email or None
        u.phone = clean(request.form.get("phone"), 30) or None
        u.office_id = office_id if office_id and db.session.get(Office, office_id) else None
        b, a = audit.diff(before, u.to_audit())
        if a:
            audit.log("update", "user", u.id, b, a)
        db.session.commit()
        flash("사용자 정보를 저장했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    u = _user_or_404(uid)
    if u.id == current_user.id:
        flash("로그인 중인 본인 계정은 삭제할 수 없습니다.", "error")
    elif u.is_admin and _active_admins() <= 1:
        flash("마지막 관리자 계정은 삭제할 수 없습니다.", "error")
    elif WorkCheck.query.filter_by(checked_by=u.id).first():
        flash("작업 전 점검 기록이 있는 계정은 삭제할 수 없습니다. 대신 [중지]하세요.", "error")
    else:
        snap = u.to_audit()
        detach_user(u)
        db.session.delete(u)
        audit.log("delete", "user", uid, snap, None)
        db.session.commit()
        flash(f"{snap['name']}({snap['login_id']}) 계정을 삭제했습니다.", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/new", methods=["POST"])
@admin_required
def user_new():
    f = request.form
    login_id = clean(f.get("login_id"), 20)
    name = clean(f.get("name"), 50)
    email = clean(f.get("email"), 120)
    role_ = f.get("role") if f.get("role") in ("user", "admin") else "user"
    office_id = f.get("office_id", type=int)
    pw = f.get("password") or ""
    err = None
    if not LOGIN_RE.match(login_id):
        err = "아이디는 영문·숫자·._- 3~20자로 입력하세요."
    elif User.query.filter(db.func.lower(User.login_id) == login_id.lower()).first():
        err = "이미 있는 아이디입니다."
    elif not name:
        err = "이름을 입력하세요."
    elif email and not EMAIL_RE.match(email):
        err = "이메일 형식이 올바르지 않습니다."
    elif password_problem(pw):
        err = "초기 " + password_problem(pw)
    if err:
        flash(err, "error")
        return redirect(url_for("admin.users"))
    u = User(login_id=login_id, name=name, email=email or None, role=role_, status="active", must_change_pw=True,
             office_id=office_id if office_id and db.session.get(Office, office_id) else None,
             approved_by=current_user.id, approved_at=datetime.now())
    u.set_password(pw)
    db.session.add(u)
    db.session.flush()
    audit.log("create", "user", u.id, None, u.to_audit())
    db.session.commit()
    flash(f"{name}({login_id}) 계정을 만들었습니다. 첫 로그인 때 비밀번호를 바꾸게 됩니다.", "ok")
    return redirect(url_for("admin.users"))


# ======================= 장비구분 기준표 =======================

@bp.route("/types", methods=["GET", "POST"])
@admin_required
def types():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "add":
            code = clean(f.get("code")).upper()
            name = clean(f.get("name"), 100)
            im, rm = f.get("inspect_months", type=int), f.get("replace_months", type=int)
            if not CODE_RE.match(code):
                flash("코드는 영문 대문자·숫자 1~6자로 입력하세요.", "error")
            elif EquipmentType.query.filter_by(code=code).first():
                flash("이미 있는 코드입니다.", "error")
            elif not name or not im or not 1 <= im <= 120:
                flash("장비구분명과 검사주기(1~120개월)를 입력하세요.", "error")
            elif rm is not None and not 1 <= rm <= 600:
                flash("교체주기는 1~600개월이어야 합니다.", "error")
            else:
                t = EquipmentType(code=code, name=name, inspect_months=im, replace_months=rm,
                                  icon=ICON_BY_CODE.get(code), sort_order=EquipmentType.query.count())
                db.session.add(t)
                db.session.flush()
                audit.log("create", "type", t.id, None, t.to_dict())
                db.session.commit()
                flash("장비구분을 추가했습니다.", "ok")
        elif action == "save":
            changed = 0
            for t in EquipmentType.query.all():
                p = f"t{t.id}_"
                if p + "name" not in f:
                    continue
                name = clean(f.get(p + "name"), 100)
                im = f.get(p + "inspect", type=int)
                rm_raw = (f.get(p + "replace") or "").strip()
                rm = int(rm_raw) if rm_raw.isdigit() else None
                active = f.get(p + "active") == "1"
                order = f.get(p + "order", type=int)
                if not name or not im or not 1 <= im <= 120 or (rm is not None and not 1 <= rm <= 600):
                    flash(f"{t.code}: 이름·검사주기(1~120)·교체주기(1~600)를 확인하세요.", "error")
                    continue
                before = t.to_dict()
                t.name, t.inspect_months, t.replace_months, t.is_active = name, im, rm, active
                if order is not None:
                    t.sort_order = order
                b, a = audit.diff(before, t.to_dict())
                if a:
                    audit.log("update", "type", t.id, b, a)
                    changed += 1
            db.session.commit()
            flash(f"기준표를 저장했습니다 ({changed}건 변경). 전체 장비의 차기검사일이 즉시 다시 계산됩니다.", "ok")
        elif action == "delete":
            t = db.get_or_404(EquipmentType, f.get("id", type=int))
            n = Equipment.query.filter_by(type_id=t.id).count()
            if n:
                flash(f"이 구분으로 등록된 장비가 {n}건 있어 삭제할 수 없습니다. [사용 안 함]으로 바꾸세요.", "error")
            else:
                audit.log("delete", "type", t.id, t.to_dict(), None)
                db.session.delete(t)
                db.session.commit()
                flash("장비구분을 삭제했습니다.", "ok")
        return redirect(url_for("admin.types"))
    rows = EquipmentType.query.order_by(EquipmentType.sort_order, EquipmentType.code).all()
    used = dict(db.session.query(Equipment.type_id, db.func.count(Equipment.id)).group_by(Equipment.type_id).all())
    return render_template("admin/types.html", rows=rows, used=used)


# ======================= 사업소 =======================

@bp.route("/offices", methods=["GET", "POST"])
@admin_required
def offices():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "add":
            code = clean(f.get("code")).upper()
            name = clean(f.get("name"), 100)
            if not CODE_RE.match(code):
                flash("코드는 영문 대문자·숫자 1~6자로 입력하세요.", "error")
            elif Office.query.filter_by(code=code).first():
                flash("이미 있는 코드입니다.", "error")
            elif not name:
                flash("사업소명을 입력하세요.", "error")
            else:
                o = Office(code=code, name=name)
                db.session.add(o)
                db.session.flush()
                audit.log("create", "office", o.id, None, o.to_dict())
                db.session.commit()
                flash("사업소를 추가했습니다.", "ok")
        elif action == "save":
            for o in Office.query.all():
                p = f"o{o.id}_"
                if p + "name" not in f:
                    continue
                name = clean(f.get(p + "name"), 100)
                if not name:
                    continue
                before = o.to_dict()
                o.name, o.is_active = name, f.get(p + "active") == "1"
                b, a = audit.diff(before, o.to_dict())
                if a:
                    audit.log("update", "office", o.id, b, a)
            db.session.commit()
            flash("사업소 정보를 저장했습니다.", "ok")
        elif action == "delete":
            o = db.get_or_404(Office, f.get("id", type=int))
            n = Equipment.query.filter_by(office_id=o.id).count()
            nu = User.query.filter_by(office_id=o.id).count()
            if n or nu:
                flash(f"이 사업소에 장비 {n}건·사용자 {nu}명이 있어 삭제할 수 없습니다. [사용 안 함]으로 바꾸세요.", "error")
            else:
                audit.log("delete", "office", o.id, o.to_dict(), None)
                db.session.delete(o)
                db.session.commit()
                flash("사업소를 삭제했습니다.", "ok")
        return redirect(url_for("admin.offices"))
    rows = Office.query.order_by(Office.code).all()
    used = dict(db.session.query(Equipment.office_id, db.func.count(Equipment.id)).group_by(Equipment.office_id).all())
    return render_template("admin/offices.html", rows=rows, used=used)


# ======================= 설정 =======================

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        if action == "logo":
            return _save_logo()
        if action == "logo_delete":
            old = cfg.get("logo_path")
            cfg.set("logo_path", "")
            audit.log("update", "setting", None, {"logo_path": old}, {"logo_path": ""})
            db.session.commit()
            if old:
                get_store().delete(os.path.basename(old))
            flash("로고를 삭제했습니다.", "ok")
            return redirect(url_for("admin.settings"))
        u, w = f.get("urgent_days", type=int), f.get("warn_days", type=int)
        t = clean(f.get("alert_time"))
        cc = clean(f.get("alert_cc"), 500)
        errs = []
        if u is None or u < 0:
            errs.append("긴급 기준은 0 이상의 숫자여야 합니다.")
        if w is None or w < 1:
            errs.append("주의 기준은 1 이상의 숫자여야 합니다.")
        if u is not None and w is not None and u >= w:
            errs.append("긴급 기준은 주의 기준보다 작아야 합니다.")
        if not TIME_RE.match(t):
            errs.append("알림 시각은 HH:MM 형식이어야 합니다 (예: 08:00).")
        bad_cc = [x for x in cc.replace(";", ",").split(",") if x.strip() and not EMAIL_RE.match(x.strip())]
        if bad_cc:
            errs.append(f"참조 수신자 이메일 형식 오류: {', '.join(bad_cc)}")
        if errs:
            for e in errs:
                flash(e, "error")
            return redirect(url_for("admin.settings"))
        new = {"urgent_days": str(u), "warn_days": str(w), "allow_signup": "1" if f.get("allow_signup") else "0",
               "alert_enabled": "1" if f.get("alert_enabled") else "0", "alert_time": t, "alert_cc": cc}
        before = {k: cfg.get(k) for k in new}
        for k, v in new.items():
            cfg.set(k, v)
        b, a = audit.diff(before, new)
        if a:
            audit.log("update", "setting", None, b, a)
        db.session.commit()
        if "alert_time" in a:
            from ..services import scheduler
            scheduler.reschedule_alert(current_app._get_current_object())
        flash("설정을 저장했습니다.", "ok")
        return redirect(url_for("admin.settings"))
    state, state_msg = alerts.mail_state()
    n_sample = Equipment.query.filter_by(is_sample=True).count()
    n_sample_users = User.query.filter(User.login_id.in_(SAMPLE_LOGINS)).count()
    return render_template("admin/settings.html", s={k: cfg.get(k) for k in cfg.DEFAULTS}, mail_state=state,
                           mail_msg=state_msg, scheduler_on=current_app.config["SCHEDULER_ENABLED"],
                           n_sample=n_sample, n_sample_users=n_sample_users,
                           base_url=current_app.config["APP_BASE_URL"])


def _save_logo():
    fs = request.files.get("logo")
    if not fs or not fs.filename:
        flash("로고 파일을 선택하세요.", "error")
        return redirect(url_for("admin.settings"))
    ext = fs.filename.rsplit(".", 1)[-1].lower() if "." in fs.filename else ""
    if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
        flash("로고는 PNG·JPG 이미지 파일만 올릴 수 있습니다.", "error")
        return redirect(url_for("admin.settings"))
    try:
        img = Image.open(fs.stream)
        img.load()
    except (UnidentifiedImageError, OSError):
        flash("이미지 파일이 아니거나 손상된 파일입니다.", "error")
        return redirect(url_for("admin.settings"))
    img.thumbnail((600, 200))
    name = f"logo_{uuid.uuid4().hex[:8]}.png"
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        get_store().save(name, buf.getvalue(), "image/png")
    except StorageError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.settings"))
    old = cfg.get("logo_path")
    cfg.set("logo_path", name)
    audit.log("update", "setting", None, {"logo_path": old}, {"logo_path": name})
    db.session.commit()
    if old:
        get_store().delete(os.path.basename(old))
    flash("로고를 등록했습니다.", "ok")
    return redirect(url_for("admin.settings"))


@bp.route("/samples/delete", methods=["POST"])
@admin_required
def samples_delete():
    n_eq, n_u = delete_samples(current_user.id)
    audit.log("delete_samples", "equipment", None, None, {"equipment": n_eq, "users": n_u})
    db.session.commit()
    flash(f"샘플 장비 {n_eq}건과 예시 계정 {n_u}개를 삭제했습니다.", "ok")
    return redirect(url_for("admin.settings"))


# ======================= 가져오기 / 내보내기 =======================

# 미리보기 결과는 DB(settings 테이블, key=imp_<token>)에 잠시 보관한다.
# 서버리스(Vercel)에서는 요청마다 다른 서버가 처리할 수 있어 임시 파일을 쓸 수 없기 때문.

def _preview_key(token):
    return f"imp_{token}"


def _preview_drop(token):
    row = db.session.get(Setting, _preview_key(token))
    if row:
        db.session.delete(row)
        db.session.commit()


@bp.route("/import", methods=["GET", "POST"])
@admin_required
def import_():
    if request.method == "POST":
        action = request.form.get("action", "upload")
        if action == "confirm":
            token = session.get("import_token")
            row = db.session.get(Setting, _preview_key(token)) if token else None
            if not token or request.form.get("token") != token or row is None:
                flash("미리보기 정보가 만료되었습니다. 파일을 다시 올려 주세요.", "error")
                return redirect(url_for("admin.import_"))
            data = json.loads(row.value)
            try:
                new, upd = io_excel.apply(data["items"], current_user.id)
            except Exception as e:  # noqa: BLE001
                current_app.logger.exception("import failed")
                flash(f"반영 중 오류가 발생해 전체를 취소했습니다: {e}", "error")
                return redirect(url_for("admin.import_"))
            finally:
                _preview_drop(token)
                session.pop("import_token", None)
            flash(f"일괄 등록을 반영했습니다: 신규 {new}건, 갱신 {upd}건"
                  + (f" (오류 {data['counts']['error']}행은 제외)" if data["counts"]["error"] else ""), "ok")
            return redirect(url_for("equipment.index"))
        if action == "cancel":
            token = session.pop("import_token", None)
            if token:
                _preview_drop(token)
            return redirect(url_for("admin.import_"))
        fs = request.files.get("file")
        if not fs or not fs.filename:
            flash("CSV 또는 XLSX 파일을 선택하세요.", "error")
            return redirect(url_for("admin.import_"))
        try:
            rows = io_excel.read_table(fs.filename, fs.read())
            data = io_excel.analyze(rows)
        except io_excel.ImportError_ as e:
            flash(str(e), "error")
            return redirect(url_for("admin.import_"))
        old = session.get("import_token")
        if old:
            _preview_drop(old)
        token = uuid.uuid4().hex
        db.session.add(Setting(key=_preview_key(token), value=json.dumps(data, ensure_ascii=False)))
        db.session.commit()
        session["import_token"] = token
        return render_template("admin/import.html", preview=data, token=token, filename=fs.filename)
    return render_template("admin/import.html", preview=None)


@bp.route("/import/template.<fmt>")
@admin_required
def import_template(fmt):
    offices, types = lookups()
    on = offices[0].name if offices else "서울사업소"
    tn = types[0].name if types else "절연장갑"
    if fmt == "xlsx":
        return Response(io_excel.template_xlsx(on, tn), headers=_dl("안전장비_등록양식.xlsx"),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return Response(io_excel.template_csv(on, tn), headers=_dl("안전장비_등록양식.csv"), mimetype="text/csv")


def _dl(name):
    from urllib.parse import quote
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@bp.route("/export.<fmt>")
@admin_required
def export(fmt):
    from ..equipment import filtered_rows
    rows, _ = filtered_rows(request.args)
    stamp = date.today().isoformat()
    audit.log("export", "equipment", None, None, {"format": fmt, "count": len(rows)})
    db.session.commit()
    if fmt == "xlsx":
        return Response(io_excel.to_xlsx(rows), headers=_dl(f"안전장비관리대장_{stamp}.xlsx"),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "csv":
        return Response(io_excel.to_csv(rows), headers=_dl(f"안전장비관리대장_{stamp}.csv"),
                        mimetype="text/csv; charset=utf-8")
    return redirect(url_for("equipment.index"))


# ======================= 백업 =======================

@bp.route("/backup", methods=["GET", "POST"])
@admin_required
def backup():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "db":
            try:
                p = backup_db()
                audit.log("backup", "backup", None, None, {"file": os.path.basename(p)})
                db.session.commit()
                flash(f"DB 백업을 만들었습니다: {os.path.basename(p)}", "ok")
            except BackupError as e:
                flash(str(e), "error")
        elif action == "restore":
            fs = request.files.get("file")
            if not fs or not fs.filename:
                flash("JSON 백업 파일을 선택하세요.", "error")
                return redirect(url_for("admin.backup"))
            if request.form.get("confirm") != "복원":
                flash("복원하려면 확인란에 '복원'을 입력하세요.", "error")
                return redirect(url_for("admin.backup"))
            try:
                data = json.loads(fs.read().decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                flash("JSON 파일을 읽을 수 없습니다.", "error")
                return redirect(url_for("admin.backup"))
            try:
                # 복원 전 자동으로 DB 파일 백업
                try:
                    backup_db()
                except BackupError:
                    pass
                if isinstance(data, dict) and data.get("app") == APP_ID:
                    n, u, d = restore_json(data, current_user.id)
                    flash(f"복원했습니다: 신규 {n}건, 갱신 {u}건, 삭제 {d}건 (계정은 그대로 유지)", "ok")
                elif isinstance(data, dict) and "items" in data:
                    r = import_legacy(data, current_user.id)
                    flash(f"시제품 백업을 가져왔습니다: 신규 {r['new']}건, 갱신 {r['update']}건, 사진 {r['photos']}건"
                          + (f", 오류 {len(r['errors'])}건" if r["errors"] else ""), "ok")
                    for e in r["errors"][:20]:
                        flash(e, "error")
                else:
                    flash("알 수 없는 백업 형식입니다.", "error")
            except BackupError as e:
                flash(str(e), "error")
        return redirect(url_for("admin.backup"))
    from ..services.backup import db_file_path
    is_sqlite = bool(db_file_path())
    return render_template("admin/backup.html", backups=list_db_backups() if is_sqlite else [], is_sqlite=is_sqlite)


@bp.route("/backup/download.json")
@admin_required
def backup_json():
    data = export_json()
    audit.log("backup", "backup", None, None, {"format": "json", "equipment": len(data["equipment"])})
    db.session.commit()
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(body, mimetype="application/json",
                    headers=_dl(f"안전장비관리대장_백업_{date.today().isoformat()}.json"))


@bp.route("/backup/download.xlsx")
@admin_required
def backup_xlsx():
    from ..services.ledger import all_rows, sort_rows
    rows = sort_rows(all_rows(), "mgmt_no")
    return Response(io_excel.to_xlsx(rows), headers=_dl(f"안전장비관리대장_백업_{date.today().isoformat()}.xlsx"),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/backup/db/<name>")
@admin_required
def backup_db_file(name):
    from ..services.backup import backup_dir
    import re
    if not re.fullmatch(r"ledger_\d{8}\.db", name):
        return redirect(url_for("admin.backup"))
    p = os.path.join(backup_dir(), name)
    if not os.path.isfile(p):
        return redirect(url_for("admin.backup"))
    return send_file(p, as_attachment=True, download_name=name)


# ======================= 감사 로그 =======================

ACTION_LABEL = {
    "create": "등록", "update": "수정", "delete": "삭제", "inspect": "검사 등록", "inspect_delete": "검사 이력 삭제",
    "photo": "사진 등록", "photo_delete": "사진 삭제", "approve": "가입 승인", "reject": "가입 반려",
    "suspend": "사용 중지", "activate": "사용 재개", "reset_password": "비밀번호 초기화", "role": "권한 변경",
    "login_fail": "로그인 실패", "login_lock": "로그인 잠금", "signup": "회원가입 신청", "password_change": "비밀번호 변경",
    "import": "가져오기", "export": "내보내기", "restore": "복원", "backup": "백업", "work_check": "작업 전 점검",
    "delete_samples": "샘플 삭제",
}
TARGET_LABEL = {"equipment": "장비", "user": "사용자", "type": "장비구분", "office": "사업소", "setting": "설정",
                "backup": "백업", "legacy": "시제품 데이터", "work_check": "작업 전 점검"}


@bp.route("/audit")
@admin_required
def audit_logs():
    page = max(request.args.get("page", 1, type=int), 1)
    q = AuditLog.query.order_by(AuditLog.at.desc(), AuditLog.id.desc())
    if request.args.get("action"):
        q = q.filter(AuditLog.action == request.args["action"])
    if request.args.get("target"):
        q = q.filter(AuditLog.target_type == request.args["target"])
    if request.args.get("user"):
        q = q.join(User, AuditLog.user_id == User.id).filter(User.login_id == request.args["user"])
    pg = q.paginate(page=page, per_page=50, error_out=False)
    targets = {}
    eq_ids = [a.target_id for a in pg.items if a.target_type == "equipment" and a.target_id]
    if eq_ids:
        targets = dict(db.session.query(Equipment.id, Equipment.mgmt_no).filter(Equipment.id.in_(eq_ids)).all())
    return render_template("admin/audit.html", pg=pg, ACTION_LABEL=ACTION_LABEL, TARGET_LABEL=TARGET_LABEL,
                           eq_names=targets,
                           args={k: v for k, v in request.args.items() if k != "page" and v})
