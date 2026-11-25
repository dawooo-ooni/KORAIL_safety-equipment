"""장비 대장: 목록, 상세, 등록/수정/삭제, 검사 등록, 사진, 대장 인쇄."""
import io
from datetime import date
from math import ceil

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy.orm.exc import StaleDataError

from ..extensions import db
from ..models import RESULTS, STATUSES, AuditLog, Equipment, EquipmentType, Inspection, Office, Photo, User
from ..services import audit
from ..services import settings as cfg
from ..services.calc import evaluate
from ..services.ledger import (SORT_KEYS, all_rows, filter_rows, grade_counts, lookups, save_with_new_no,
                               sort_rows, status_of, sync_latest_inspection)
from ..services.photos import PhotoError, read_file, remove_file, save_upload
from ..services.qr import qr_url
from ..utils import EMAIL_RE, admin_required, clean, parse_date

bp = Blueprint("equipment", __name__)

FILTER_KEYS = ("q", "office", "type", "grade", "status")


def filtered_rows(args):
    rows = filter_rows(all_rows(), args)
    sort = args.get("sort") if args.get("sort") in SORT_KEYS else "eff"
    return sort_rows(rows, sort, desc=args.get("dir") == "desc"), sort


@bp.route("/equipment")
@login_required
def index():
    rows, sort = filtered_rows(request.args)
    per_page = current_app.config["PER_PAGE"]
    pages = max(1, ceil(len(rows) / per_page))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    offices, types = lookups()
    args = {k: request.args.get(k, "") for k in FILTER_KEYS}
    return render_template(
        "equipment/index.html", rows=rows[(page - 1) * per_page: page * per_page], total_filtered=len(rows),
        total=Equipment.query.count(), page=page, pages=pages, sort=sort, desc=request.args.get("dir") == "desc",
        offices=offices, types=types, statuses=STATUSES, args=args, filtered=any(args.values()),
    )


@bp.route("/equipment/<int:eid>")
@login_required
def detail(eid):
    eq = db.get_or_404(Equipment, eid)
    st = status_of(eq)
    changes = []
    if current_user.is_admin:
        changes = (AuditLog.query.filter_by(target_type="equipment", target_id=eq.id)
                   .order_by(AuditLog.at.desc()).limit(100).all())
    return render_template("equipment/detail.html", eq=eq, st=st, changes=changes, qr_target=qr_url(eq.mgmt_no),
                           scanned=request.args.get("scan") == "1")


# ---------------- 등록/수정 ----------------

def _form_values(f):
    return {
        "office_id": f.get("office_id", type=int), "type_id": f.get("type_id", type=int),
        "last_inspect_date": clean(f.get("last_inspect_date")), "last_result": f.get("last_result") or "합격",
        "model": clean(f.get("model"), 100), "serial_no": clean(f.get("serial_no"), 100),
        "issue_date": clean(f.get("issue_date")), "status": f.get("status") or "사용중",
        "manager_user_id": f.get("manager_user_id", type=int), "manager_name": clean(f.get("manager_name"), 50),
        "manager_email": clean(f.get("manager_email"), 120), "location": clean(f.get("location"), 100),
        "note": clean(f.get("note"), 300), "version": f.get("version", type=int),
    }


def _validate(v, is_new):
    """서버 측 검증 (4.13). 반환: (오류 목록, 정리된 값)"""
    errs = []
    today = date.today()
    office = db.session.get(Office, v["office_id"]) if v["office_id"] else None
    etype = db.session.get(EquipmentType, v["type_id"]) if v["type_id"] else None
    if not office:
        errs.append("사업소를 선택하세요.")
    elif is_new and not office.is_active:
        errs.append("사용하지 않는 사업소입니다.")
    if not etype:
        errs.append("장비구분을 선택하세요.")
    elif is_new and not etype.is_active:
        errs.append("사용하지 않는 장비구분입니다.")
    last = parse_date(v["last_inspect_date"])
    if not v["last_inspect_date"]:
        errs.append("최종검사일을 입력하세요.")
    elif not last:
        errs.append("최종검사일 형식이 올바르지 않습니다 (YYYY-MM-DD).")
    elif last > today:
        errs.append("최종검사일이 오늘보다 늦습니다. 날짜를 확인하세요.")
    issue = parse_date(v["issue_date"]) if v["issue_date"] else None
    if v["issue_date"] and not issue:
        errs.append("지급일 형식이 올바르지 않습니다 (YYYY-MM-DD).")
    elif issue and issue > today:
        errs.append("지급일이 오늘보다 늦습니다.")
    if v["last_result"] not in RESULTS:
        errs.append("최종검사결과가 올바르지 않습니다.")
    if v["status"] not in STATUSES:
        errs.append("상태가 올바르지 않습니다.")
    mgr = None
    if v["manager_user_id"]:
        mgr = db.session.get(User, v["manager_user_id"])
        if not mgr or mgr.status != "active":
            errs.append("담당자 계정을 찾을 수 없습니다.")
    if v["manager_email"] and not EMAIL_RE.match(v["manager_email"]):
        errs.append("담당자 이메일 형식이 올바르지 않습니다.")
    return errs, {"office": office, "etype": etype, "last": last, "issue": issue, "mgr": mgr}


def _render_form(eq, v, errors=None, conflict=None):
    offices, types = lookups()
    users = User.query.filter_by(status="active").order_by(User.name).all()
    return render_template("equipment/form.html", eq=eq, v=v, errors=errors or [], conflict=conflict,
                           offices=offices, types=types, users=users, statuses=STATUSES, results=RESULTS)


def _eq_to_form(eq):
    return {
        "office_id": eq.office_id, "type_id": eq.type_id,
        "last_inspect_date": eq.last_inspect_date.isoformat() if eq.last_inspect_date else "",
        "last_result": eq.last_result, "model": eq.model or "", "serial_no": eq.serial_no or "",
        "issue_date": eq.issue_date.isoformat() if eq.issue_date else "", "status": eq.status,
        "manager_user_id": eq.manager_user_id, "manager_name": eq.manager_name or "",
        "manager_email": eq.manager_email or "", "location": eq.location or "", "note": eq.note or "",
        "version": eq.version,
    }


@bp.route("/equipment/new", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "GET":
        return _render_form(None, {"last_result": "합격", "status": "사용중",
                                   "office_id": request.args.get("office_id", type=int)})
    v = _form_values(request.form)
    errs, c = _validate(v, True)
    if errs:
        return _render_form(None, v, errs), 400
    eq = Equipment(
        office=c["office"], etype=c["etype"], last_inspect_date=c["last"], last_result=v["last_result"],
        model=v["model"] or None, serial_no=v["serial_no"] or None, issue_date=c["issue"], status=v["status"],
        manager_user_id=c["mgr"].id if c["mgr"] else None, manager_name=v["manager_name"] or None,
        manager_email=v["manager_email"] or None, location=v["location"] or None, note=v["note"] or None,
        created_by=current_user.id, updated_by=current_user.id,
    )
    save_with_new_no(eq, c["office"].code, c["etype"].code)
    db.session.add(Inspection(equipment_id=eq.id, inspect_date=c["last"], result=v["last_result"],
                              memo="최초 등록", created_by=current_user.id))
    audit.log("create", "equipment", eq.id, None, eq.to_audit())
    db.session.commit()
    flash(f"{eq.mgmt_no} 장비를 등록했습니다.", "ok")
    return redirect(url_for("equipment.detail", eid=eq.id))


@bp.route("/equipment/<int:eid>/edit", methods=["GET", "POST"])
@admin_required
def edit(eid):
    eq = db.get_or_404(Equipment, eid)
    if request.method == "GET":
        return _render_form(eq, _eq_to_form(eq))
    v = _form_values(request.form)
    errs, c = _validate(v, False)
    if errs:
        v["version"] = v["version"] or eq.version
        return _render_form(eq, v, errs), 400
    if v["version"] != eq.version:
        # 다른 사람이 먼저 저장함 (4.12) → 저장 막고 최신 값 안내
        v["version"] = eq.version
        return _render_form(eq, v, ["다른 사용자가 이 장비를 먼저 수정했습니다. 아래 최신 저장값을 확인한 뒤 다시 저장하세요."],
                            conflict=_eq_to_form(eq)), 409
    before = eq.to_audit()
    old_prefix = (eq.office.code, eq.etype.code)
    eq.office, eq.etype = c["office"], c["etype"]
    eq.last_inspect_date, eq.last_result = c["last"], v["last_result"]
    eq.model, eq.serial_no, eq.issue_date = v["model"] or None, v["serial_no"] or None, c["issue"]
    eq.status = v["status"]
    eq.manager_user_id = c["mgr"].id if c["mgr"] else None
    eq.manager_name, eq.manager_email = v["manager_name"] or None, v["manager_email"] or None
    eq.location, eq.note = v["location"] or None, v["note"] or None
    eq.is_sample = False
    eq.updated_by = current_user.id
    try:
        if old_prefix != (c["office"].code, c["etype"].code):
            save_with_new_no(eq, c["office"].code, c["etype"].code)
        db.session.flush()
    except StaleDataError:
        db.session.rollback()
        eq = db.get_or_404(Equipment, eid)
        v["version"] = eq.version
        return _render_form(eq, v, ["다른 사용자가 이 장비를 먼저 수정했습니다. 최신 값을 확인한 뒤 다시 저장하세요."],
                            conflict=_eq_to_form(eq)), 409
    b, a = audit.diff(before, eq.to_audit())
    if a:
        audit.log("update", "equipment", eq.id, b, a)
    db.session.commit()
    flash("저장했습니다.", "ok")
    return redirect(url_for("equipment.detail", eid=eq.id))


@bp.route("/equipment/preview")
@login_required
def preview():
    """등록 화면의 자동 계산 미리보기 (JSON)"""
    t = db.session.get(EquipmentType, request.args.get("type_id", type=int) or 0)
    last = parse_date(request.args.get("last"))
    if not t or not last:
        return jsonify(ok=False)

    class _E:
        pass
    e = _E()
    e.last_inspect_date, e.issue_date = last, parse_date(request.args.get("issue"))
    e.last_result = request.args.get("result") or "합격"
    e.status = request.args.get("status") or "사용중"
    u, w = cfg.thresholds()
    st = evaluate(e, t, date.today(), u, w)
    from ..services.calc import GRADES, dd_text
    return jsonify(ok=True, next=st.next_inspect.isoformat(), dd=dd_text(st.days_left),
                   repl=st.replace_due.isoformat() if st.replace_due else None,
                   grade=st.grade, label=GRADES[st.grade]["label"], icon=GRADES[st.grade]["icon"],
                   css=GRADES[st.grade]["css"], reason=st.reason)


@bp.route("/equipment/<int:eid>/delete", methods=["POST"])
@admin_required
def delete(eid):
    eq = db.get_or_404(Equipment, eid)
    if request.form.get("confirm_no", "").strip().upper() != eq.mgmt_no:
        flash("삭제하려면 관리번호를 정확히 입력하세요.", "error")
        return redirect(url_for("equipment.detail", eid=eid))
    snap = eq.to_audit()
    snap["inspections"] = len(eq.inspections)
    files = [p.file_path for p in eq.photos]
    db.session.delete(eq)
    audit.log("delete", "equipment", eid, snap, None)
    db.session.commit()
    for fp in files:
        remove_file(fp)
    flash(f"{snap['mgmt_no']} 장비를 삭제했습니다.", "ok")
    return redirect(url_for("equipment.index"))


# ---------------- 검사 등록 ----------------

@bp.route("/equipment/<int:eid>/inspections", methods=["GET", "POST"])
@admin_required
def inspect(eid):
    eq = db.get_or_404(Equipment, eid)
    if request.method == "GET":
        return render_template("equipment/inspect.html", eq=eq, f={}, errors=[])
    f = request.form
    d = parse_date(f.get("inspect_date"))
    result = f.get("result")
    errors = []
    if not d:
        errors.append("검사일을 입력하세요 (YYYY-MM-DD).")
    elif d > date.today():
        errors.append("검사일이 오늘보다 늦습니다.")
    if result not in RESULTS:
        errors.append("검사결과를 선택하세요.")
    if errors:
        return render_template("equipment/inspect.html", eq=eq, f=f, errors=errors), 400
    before = {"last_inspect_date": eq.last_inspect_date.isoformat(), "last_result": eq.last_result}
    insp = Inspection(equipment_id=eq.id, inspect_date=d, result=result, inspector=clean(f.get("inspector"), 100) or None,
                      memo=clean(f.get("memo"), 300) or None, created_by=current_user.id)
    db.session.add(insp)
    db.session.flush()
    sync_latest_inspection(eq)
    eq.is_sample = False
    eq.updated_by = current_user.id
    db.session.flush()
    audit.log("inspect", "equipment", eq.id, before,
              {"inspect_date": d.isoformat(), "result": result, "inspector": insp.inspector,
               "last_inspect_date": eq.last_inspect_date.isoformat(), "last_result": eq.last_result})
    db.session.commit()
    st = status_of(eq)
    msg = f"검사 결과를 등록했습니다. 차기검사일: {st.next_inspect.isoformat()}"
    if eq.last_result == "불합격":
        msg += " — 불합격으로 사용불가 처리됩니다."
    flash(msg, "ok")
    return redirect(url_for("equipment.detail", eid=eq.id))


@bp.route("/equipment/<int:eid>/inspections/<int:iid>/delete", methods=["POST"])
@admin_required
def inspect_delete(eid, iid):
    eq = db.get_or_404(Equipment, eid)
    insp = db.get_or_404(Inspection, iid)
    if insp.equipment_id != eq.id:
        abort(404)
    if len(eq.inspections) <= 1:
        flash("검사 이력이 1건뿐이라 삭제할 수 없습니다.", "error")
        return redirect(url_for("equipment.detail", eid=eid))
    snap = {"inspect_date": insp.inspect_date.isoformat(), "result": insp.result, "inspector": insp.inspector,
            "memo": insp.memo}
    db.session.delete(insp)
    db.session.flush()
    db.session.expire(eq, ["inspections"])
    sync_latest_inspection(eq)
    eq.updated_by = current_user.id
    audit.log("inspect_delete", "equipment", eq.id, snap, None)
    db.session.commit()
    flash("검사 이력을 삭제했습니다.", "ok")
    return redirect(url_for("equipment.detail", eid=eid))


# ---------------- 사진 ----------------

@bp.route("/equipment/<int:eid>/photo", methods=["POST"])
@admin_required
def photo(eid):
    eq = db.get_or_404(Equipment, eid)
    if request.form.get("_method") == "DELETE":
        return photo_delete(eid)
    try:
        rel, w, h, size = save_upload(request.files.get("photo"))
    except PhotoError as e:
        flash(str(e), "error")
        return redirect(url_for("equipment.detail", eid=eid))
    old = [(p.id, p.file_path) for p in eq.photos]
    for p in list(eq.photos):
        db.session.delete(p)
    ph = Photo(equipment_id=eq.id, file_path=rel, width=w, height=h, size_bytes=size, is_primary=True,
               uploaded_by=current_user.id)
    db.session.add(ph)
    audit.log("photo", "equipment", eq.id, {"photos": [o[1] for o in old]} if old else None,
              {"file": rel, "width": w, "height": h, "size": size})
    db.session.commit()
    for _, fp in old:
        remove_file(fp)
    flash("사진을 등록했습니다." if not old else "사진을 바꿨습니다.", "ok")
    return redirect(url_for("equipment.detail", eid=eid))


@bp.route("/equipment/<int:eid>/photo", methods=["DELETE"])
@admin_required
def photo_delete(eid):
    eq = db.get_or_404(Equipment, eid)
    files = [p.file_path for p in eq.photos]
    for p in list(eq.photos):
        db.session.delete(p)
    if files:
        audit.log("photo_delete", "equipment", eq.id, {"photos": files}, None)
    db.session.commit()
    for fp in files:
        remove_file(fp)
    if request.method == "DELETE":
        return jsonify(ok=True)
    flash("사진을 삭제했습니다. 예시 그림이 표시됩니다.", "ok")
    return redirect(url_for("equipment.detail", eid=eid))


@bp.route("/photos/<int:pid>")
@login_required
def photo_file(pid):
    p = db.get_or_404(Photo, pid)
    data = read_file(p.file_path)
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=3600)


# ---------------- 대장 인쇄 ----------------

@bp.route("/print/ledger")
@login_required
def print_ledger():
    rows, _ = filtered_rows(request.args)
    office = Office.query.filter_by(code=request.args.get("office")).first() if request.args.get("office") else None
    return render_template("print/ledger.html", rows=rows, cnt=grade_counts(rows), office=office)
