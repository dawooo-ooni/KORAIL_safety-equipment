"""백업·복원 (작업지시서 4.10, 9.3).
- JSON 백업/복원 (사업소·기준표·장비·검사 이력·설정, 계정 제외)
- 시제품 JSON 백업 가져오기 (import-legacy)
- SQLite 파일 백업 (sqlite3 backup API, 30일 보관)
"""
import base64
import io
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import current_app
from werkzeug.datastructures import FileStorage

from ..extensions import db
from ..models import Equipment, EquipmentType, Inspection, Office, Photo
from . import audit
from . import settings as cfg
from .ledger import ICON_BY_CODE, next_mgmt_no
from .photos import PhotoError, remove_file, save_upload
from ..utils import CODE_RE, MGMT_NO_RE, parse_date

APP_ID = "korail-ledger-flask"
SETTING_KEYS = ("urgent_days", "warn_days", "allow_signup", "alert_enabled", "alert_time", "alert_cc")


class BackupError(ValueError):
    pass


def _d(v):
    return v.isoformat() if v else None


# ---------------- JSON 백업 ----------------

def export_json():
    offices = Office.query.order_by(Office.code).all()
    types = EquipmentType.query.order_by(EquipmentType.sort_order, EquipmentType.code).all()
    eqs = Equipment.query.order_by(Equipment.mgmt_no).all()
    return {
        "app": APP_ID, "version": 1, "exported_at": datetime.now().isoformat(timespec="seconds"),
        "settings": {k: cfg.get(k) for k in SETTING_KEYS},
        "offices": [o.to_dict() for o in offices],
        "types": [t.to_dict() for t in types],
        "equipment": [
            {
                "mgmt_no": e.mgmt_no, "office": e.office.code, "type": e.etype.code, "model": e.model,
                "serial_no": e.serial_no, "issue_date": _d(e.issue_date), "last_inspect_date": _d(e.last_inspect_date),
                "last_result": e.last_result, "status": e.status, "manager_name": e.manager_display or None,
                "manager_email": e.manager_mail or None, "location": e.location, "note": e.note,
                "is_sample": e.is_sample,
                "inspections": [
                    {"inspect_date": _d(i.inspect_date), "result": i.result, "inspector": i.inspector, "memo": i.memo}
                    for i in sorted(e.inspections, key=lambda x: (x.inspect_date, x.id))
                ],
            }
            for e in eqs
        ],
    }


def _upsert_office(code, name, is_active=True):
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        raise BackupError(f"사업소 코드 형식 오류: {code}")
    o = Office.query.filter_by(code=code).first()
    if o:
        o.name = name or o.name
        o.is_active = bool(is_active)
    else:
        o = Office(code=code, name=name or code, is_active=bool(is_active))
        db.session.add(o)
    db.session.flush()
    return o


def _upsert_type(code, name, inspect_months, replace_months=None, icon=None, sort_order=None, is_active=True):
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        raise BackupError(f"장비구분 코드 형식 오류: {code}")
    try:
        im = int(inspect_months)
    except (TypeError, ValueError):
        im = 6
    im = min(max(im, 1), 120)
    try:
        rm = int(replace_months) if replace_months not in (None, "") else None
    except (TypeError, ValueError):
        rm = None
    if rm is not None and not 1 <= rm <= 600:
        rm = None
    t = EquipmentType.query.filter_by(code=code).first()
    if t:
        t.name, t.inspect_months, t.replace_months = name or t.name, im, rm
        t.is_active = bool(is_active)
        if icon:
            t.icon = icon
        if sort_order is not None:
            t.sort_order = sort_order
    else:
        t = EquipmentType(code=code, name=name or code, inspect_months=im, replace_months=rm,
                          icon=icon or ICON_BY_CODE.get(code),
                          sort_order=sort_order if sort_order is not None else EquipmentType.query.count(),
                          is_active=bool(is_active))
        db.session.add(t)
    db.session.flush()
    return t


def _apply_equipment(rec, offices, types, user_id, today):
    """rec: 공통 형식 dict. 반환: (Equipment, created?)"""
    o = offices.get((rec.get("office") or "").upper())
    t = types.get((rec.get("type") or "").upper())
    if not o or not t:
        raise BackupError(f"{rec.get('mgmt_no') or '(번호 없음)'}: 사업소/장비구분 코드를 찾을 수 없습니다")
    last = parse_date(rec.get("last_inspect_date"))
    if not last:
        raise BackupError(f"{rec.get('mgmt_no')}: 최종검사일이 없습니다")
    no = (rec.get("mgmt_no") or "").strip().upper()
    if no and not MGMT_NO_RE.match(no):
        no = ""
    eq = Equipment.query.filter_by(mgmt_no=no).first() if no else None
    created = eq is None
    if created:
        eq = Equipment(created_by=user_id)
        eq.mgmt_no = no or next_mgmt_no(o.code, t.code)
        db.session.add(eq)
    eq.office, eq.etype = o, t
    eq.model = (rec.get("model") or None)
    eq.serial_no = rec.get("serial_no") or None
    eq.issue_date = parse_date(rec.get("issue_date"))
    eq.last_inspect_date = last
    eq.last_result = rec.get("last_result") if rec.get("last_result") in ("합격", "불합격") else "합격"
    eq.status = rec.get("status") if rec.get("status") in ("사용중", "보관", "수리중", "폐기") else "사용중"
    eq.manager_user_id = None
    eq.manager_name = (rec.get("manager_name") or None)
    eq.manager_email = (rec.get("manager_email") or None)
    eq.location = rec.get("location") or None
    eq.note = rec.get("note") or None
    eq.is_sample = bool(rec.get("is_sample"))
    eq.updated_by = user_id
    db.session.flush()
    # 검사 이력 교체
    Inspection.query.filter_by(equipment_id=eq.id).delete()
    hist = rec.get("inspections") or []
    for h in hist:
        d = parse_date(h.get("inspect_date"))
        if not d:
            continue
        db.session.add(Inspection(equipment_id=eq.id, inspect_date=d,
                                  result=h.get("result") if h.get("result") in ("합격", "불합격") else "합격",
                                  inspector=(h.get("inspector") or None), memo=(h.get("memo") or None),
                                  created_by=user_id))
    if not hist:
        db.session.add(Inspection(equipment_id=eq.id, inspect_date=last, result=eq.last_result,
                                  memo="복원", created_by=user_id))
    db.session.flush()
    return eq, created


def restore_json(data, user_id=None, replace=True):
    """우리 앱 JSON 백업 복원. replace=True 면 백업에 없는 장비는 삭제한다."""
    if data.get("app") != APP_ID:
        raise BackupError("이 앱의 JSON 백업 파일이 아닙니다.")
    today = date.today()
    try:
        for o in data.get("offices", []):
            _upsert_office(o.get("code"), o.get("name"), o.get("is_active", True))
        for t in data.get("types", []):
            _upsert_type(t.get("code"), t.get("name"), t.get("inspect_months"), t.get("replace_months"),
                         t.get("icon"), t.get("sort_order"), t.get("is_active", True))
        offices = {o.code: o for o in Office.query.all()}
        types = {t.code: t for t in EquipmentType.query.all()}
        keep = set()
        n_new = n_upd = 0
        for rec in data.get("equipment", []):
            eq, created = _apply_equipment(rec, offices, types, user_id, today)
            keep.add(eq.id)
            n_new += created
            n_upd += not created
        n_del = 0
        if replace:
            q = Equipment.query.filter(~Equipment.id.in_(keep)) if keep else Equipment.query
            for eq in q.all():
                for p in eq.photos:
                    remove_file(p.file_path)
                db.session.delete(eq)
                n_del += 1
        for k, v in (data.get("settings") or {}).items():
            if k in SETTING_KEYS:
                cfg.set(k, v)
        audit.log("restore", "backup", None, None,
                  {"new": n_new, "update": n_upd, "delete": n_del}, user_id=user_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return n_new, n_upd, n_del


# ---------------- 시제품 JSON 가져오기 ----------------

def import_legacy(data, user_id=None, with_photos=True):
    """시제품(HTML) JSON 백업 → 사업소·기준표·장비·검사 이력 (계정 제외). 기존 장비는 관리번호로 갱신."""
    if not isinstance(data, dict) or "items" not in data:
        raise BackupError("시제품 JSON 백업 형식이 아닙니다 (items 항목 없음).")
    st = data.get("settings") or {}
    today = date.today()
    n_new = n_upd = n_photo = 0
    errors = []
    try:
        for o in st.get("offices") or []:
            _upsert_office(o.get("code"), o.get("name"))
        for i, t in enumerate(st.get("types") or []):
            _upsert_type(t.get("code"), t.get("name"), t.get("inspectMonths"), t.get("replaceMonths"), sort_order=i)
        if str(st.get("urgentDays", "")).isdigit():
            cfg.set("urgent_days", st["urgentDays"])
        if str(st.get("warnDays", "")).isdigit():
            cfg.set("warn_days", st["warnDays"])
        if "allowSignup" in st:
            cfg.set("allow_signup", "1" if st["allowSignup"] else "0")
        offices = {o.code: o for o in Office.query.all()}
        types = {t.code: t for t in EquipmentType.query.all()}
        photos = data.get("photos") or {}
        for it in data.get("items") or []:
            rec = {
                "mgmt_no": it.get("no"), "office": it.get("office"), "type": it.get("type"),
                "model": it.get("model"), "serial_no": it.get("serial"), "issue_date": it.get("issueDate"),
                "last_inspect_date": it.get("lastInspect"), "last_result": it.get("result"),
                "status": it.get("status"), "manager_name": it.get("manager"), "manager_email": it.get("email"),
                "location": it.get("location"), "note": it.get("note"), "is_sample": bool(it.get("sample")),
                "inspections": [
                    {"inspect_date": h.get("date"), "result": h.get("result"), "inspector": h.get("inspector"),
                     "memo": h.get("memo")}
                    for h in (it.get("history") or [])
                ],
            }
            sp = db.session.begin_nested()
            try:
                eq, created = _apply_equipment(rec, offices, types, user_id, today)
                sp.commit()
            except BackupError as e:
                sp.rollback()
                errors.append(str(e))
                continue
            n_new += created
            n_upd += not created
            url = photos.get(it.get("id")) if with_photos else None
            if url and isinstance(url, str) and url.startswith("data:image"):
                try:
                    raw = base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", url))
                    fs = FileStorage(stream=io.BytesIO(raw), filename="legacy.jpg")
                    rel, w, h, size = save_upload(fs)
                    for p in eq.photos:
                        remove_file(p.file_path)
                        db.session.delete(p)
                    db.session.add(Photo(equipment_id=eq.id, file_path=rel, width=w, height=h, size_bytes=size,
                                         is_primary=True, uploaded_by=user_id))
                    n_photo += 1
                except (PhotoError, ValueError):
                    errors.append(f"{eq.mgmt_no}: 사진을 옮기지 못했습니다")
        audit.log("import", "legacy", None, None,
                  {"new": n_new, "update": n_upd, "photos": n_photo, "errors": len(errors)}, user_id=user_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {"new": n_new, "update": n_upd, "photos": n_photo, "errors": errors}


# ---------------- SQLite 파일 백업 ----------------

def backup_dir():
    d = os.path.join(current_app.instance_path, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def db_file_path():
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return url.database


def backup_db(keep_days=None):
    """instance/backups/ledger_YYYYMMDD.db 로 복사 (같은 날은 덮어씀). 오래된 백업 정리."""
    src_path = db_file_path()
    if not src_path:
        raise BackupError("SQLite 파일 DB가 아니어서 파일 백업을 할 수 없습니다.")
    keep_days = keep_days or current_app.config.get("BACKUP_KEEP_DAYS", 30)
    dst_path = os.path.join(backup_dir(), f"ledger_{date.today():%Y%m%d}.db")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    cutoff = date.today() - timedelta(days=keep_days)
    for f in os.listdir(backup_dir()):
        m = re.fullmatch(r"ledger_(\d{8})\.db", f)
        if m and datetime.strptime(m.group(1), "%Y%m%d").date() < cutoff:
            try:
                os.remove(os.path.join(backup_dir(), f))
            except OSError:
                pass
    return dst_path


def list_db_backups():
    out = []
    for f in sorted(os.listdir(backup_dir()), reverse=True):
        if re.fullmatch(r"ledger_\d{8}\.db", f):
            p = os.path.join(backup_dir(), f)
            out.append({"name": f, "size": os.path.getsize(p),
                        "mtime": datetime.fromtimestamp(os.path.getmtime(p))})
    return out
