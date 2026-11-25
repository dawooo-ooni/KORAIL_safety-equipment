"""CSV/XLSX 가져오기·내보내기 (작업지시서 8.4, 4.11)."""
import csv
import io
from datetime import date

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from ..extensions import db
from ..models import STATUSES, Equipment, EquipmentType, Inspection, Office
from . import audit
from .calc import GRADES
from .ledger import find_office, find_type, save_with_new_no
from ..utils import EMAIL_RE, MGMT_NO_RE, parse_date

COLUMNS = ["관리번호", "사업소", "장비구분", "모델/규격", "제조번호", "지급일", "최종검사일", "최종검사결과",
           "상태", "담당자", "담당자이메일", "보관위치", "비고"]
EXPORT_EXTRA = ["차기검사일", "남은일수", "교체예정일", "경보등급", "경보사유"]
FMAP = {
    "관리번호": "no", "사업소": "office", "장비구분": "type", "모델/규격": "model", "모델": "model", "규격": "model",
    "제조번호": "serial", "지급일": "issue", "제조일": "issue", "지급일(또는제조일)": "issue",
    "최종검사일": "last", "최종검사결과": "result", "검사결과": "result", "결과": "result", "상태": "status",
    "담당자": "manager", "담당자이메일": "email", "이메일": "email", "보관위치": "location", "비고": "note",
}


class ImportError_(ValueError):
    pass


# ---------------- 읽기 ----------------

def _decode(raw: bytes) -> str:
    """UTF-8(BOM 포함) → CP949 순서로 자동 판별"""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_("파일 인코딩을 알 수 없습니다. UTF-8 또는 CP949(엑셀 기본) CSV로 저장해 주세요.")


def read_table(filename: str, raw: bytes):
    """반환: 행 목록(각 행은 값 목록). 빈 행·'#' 주석 행은 제외."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        try:
            wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        except Exception:  # noqa: BLE001
            raise ImportError_("XLSX 파일을 열 수 없습니다.") from None
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = _decode(raw)
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ImportError_("CSV 또는 XLSX 파일만 가져올 수 있습니다.")
    out = []
    for r in rows:
        vals = ["" if v is None else v for v in r]
        if not any(str(v).strip() for v in vals):
            continue
        if str(vals[0]).strip().startswith("#"):
            continue
        out.append(vals)
    return out


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def analyze(rows, today=None):
    """미리보기용 분석. 반환: dict(items=[...], counts={new,update,error})
    각 item 은 JSON 직렬화 가능한 dict (확정 단계에서 그대로 사용)."""
    today = today or date.today()
    if len(rows) < 2:
        raise ImportError_("데이터 행이 없습니다.")
    head = [_s(h).replace(" ", "") for h in rows[0]]
    idx = {}
    for i, h in enumerate(head):
        k = FMAP.get(h)
        if k and k not in idx:
            idx[k] = i
    missing = [n for k, n in (("office", "사업소"), ("type", "장비구분"), ("last", "최종검사일")) if k not in idx]
    if missing:
        raise ImportError_(f"머리글에 {', '.join(missing)} 열이 있어야 합니다. [등록 양식]을 받아 사용하세요.")

    items, seen = [], set()
    for n, r in enumerate(rows[1:], start=2):
        def g(k, r=r):
            i = idx.get(k)
            return r[i] if i is not None and i < len(r) else ""
        errs = []
        office = find_office(_s(g("office")))
        etype = find_type(_s(g("type")))
        last = parse_date(g("last"))
        issue_raw = g("issue")
        issue = parse_date(issue_raw) if _s(issue_raw) else None
        if not office:
            errs.append(f"사업소 '{_s(g('office'))}'를 찾을 수 없습니다 (사업소 관리에서 먼저 추가)")
        if not etype:
            errs.append(f"장비구분 '{_s(g('type'))}'를 찾을 수 없습니다 (기준표에서 먼저 추가)")
        if not last:
            errs.append(f"최종검사일 '{_s(g('last'))}' 형식 오류")
        elif last > today:
            errs.append("최종검사일이 오늘보다 늦습니다")
        if _s(issue_raw) and not issue:
            errs.append(f"지급일 '{_s(issue_raw)}' 형식 오류")
        elif issue and issue > today:
            errs.append("지급일이 오늘보다 늦습니다")
        result = _s(g("result")) or "합격"
        if result not in ("합격", "불합격"):
            errs.append(f"최종검사결과 '{result}'는 합격/불합격 중 하나여야 합니다")
        status = _s(g("status")) or "사용중"
        if status not in STATUSES:
            errs.append(f"상태 '{status}'는 {'/'.join(STATUSES)} 중 하나여야 합니다")
        email = _s(g("email"))
        if email and not EMAIL_RE.match(email):
            errs.append(f"담당자이메일 '{email}' 형식 오류")
        no = _s(g("no")).upper()
        note = _s(g("note"))[:300]
        action = "new"
        target_id = None
        if no:
            if no in seen:
                errs.append(f"관리번호 {no}가 파일 안에서 중복됩니다")
            seen.add(no)
            ex = Equipment.query.filter_by(mgmt_no=no).first()
            if ex:
                action, target_id = "update", ex.id
            elif not MGMT_NO_RE.match(no):
                note = ((note + " / ") if note else "") + f"기존번호 {no}"
                no = ""
        items.append({
            "line": n, "action": "error" if errs else action, "errors": errs, "target_id": target_id,
            "mgmt_no": no, "office_id": office.id if office else None, "office": office.name if office else _s(g("office")),
            "type_id": etype.id if etype else None, "type": etype.name if etype else _s(g("type")),
            "model": _s(g("model"))[:100], "serial_no": _s(g("serial"))[:100],
            "issue_date": issue.isoformat() if issue else None,
            "last_inspect_date": last.isoformat() if last else None,
            "last_result": result, "status": status, "manager_name": _s(g("manager"))[:50],
            "manager_email": email[:120], "location": _s(g("location"))[:100], "note": note[:300],
        })
    counts = {k: sum(1 for i in items if i["action"] == k) for k in ("new", "update", "error")}
    return {"items": items, "counts": counts}


def apply(items, user_id):
    """확정: 오류 없는 행을 한 트랜잭션으로 반영. 반환: (신규, 갱신)"""
    new = upd = 0
    try:
        for it in items:
            if it["action"] not in ("new", "update"):
                continue
            last = date.fromisoformat(it["last_inspect_date"])
            issue = date.fromisoformat(it["issue_date"]) if it["issue_date"] else None
            fields = dict(
                office_id=it["office_id"], type_id=it["type_id"], model=it["model"] or None,
                serial_no=it["serial_no"] or None, issue_date=issue, last_inspect_date=last,
                last_result=it["last_result"], status=it["status"], manager_name=it["manager_name"] or None,
                manager_email=it["manager_email"] or None, location=it["location"] or None, note=it["note"] or None,
            )
            eq = db.session.get(Equipment, it["target_id"]) if it["action"] == "update" else None
            if eq is not None:
                before = eq.to_audit()
                old_last = eq.last_inspect_date
                office_changed = eq.office_id != fields["office_id"] or eq.type_id != fields["type_id"]
                for k, v in fields.items():
                    setattr(eq, k, v)
                eq.is_sample = False
                eq.updated_by = user_id
                if old_last != last and not Inspection.query.filter_by(equipment_id=eq.id, inspect_date=last).first():
                    db.session.add(Inspection(equipment_id=eq.id, inspect_date=last, result=it["last_result"],
                                              memo="일괄 등록(가져오기)", created_by=user_id))
                db.session.flush()
                if office_changed:
                    db.session.refresh(eq)
                    save_with_new_no(eq, eq.office.code, eq.etype.code)
                b, a = audit.diff(before, eq.to_audit())
                if a:
                    audit.log("update", "equipment", eq.id, b, a, user_id=user_id)
                upd += 1
            else:
                eq = Equipment(**fields, created_by=user_id, updated_by=user_id)
                eq.office = db.session.get(Office, fields["office_id"])
                eq.etype = db.session.get(EquipmentType, fields["type_id"])
                if it["mgmt_no"] and not Equipment.query.filter_by(mgmt_no=it["mgmt_no"]).first():
                    eq.mgmt_no = it["mgmt_no"]
                    db.session.add(eq)
                    db.session.flush()
                else:
                    save_with_new_no(eq, eq.office.code, eq.etype.code)
                db.session.add(Inspection(equipment_id=eq.id, inspect_date=last, result=it["last_result"],
                                          memo="일괄 등록(가져오기)", created_by=user_id))
                audit.log("create", "equipment", eq.id, None, eq.to_audit(), user_id=user_id)
                new += 1
        audit.log("import", "equipment", None, None, {"new": new, "update": upd}, user_id=user_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return new, upd


# ---------------- 내보내기 ----------------

def export_values(rows):
    for r in rows:
        e, st = r.eq, r.st
        yield [
            e.mgmt_no, e.office.name, e.etype.name, e.model or "", e.serial_no or "",
            e.issue_date, e.last_inspect_date, e.last_result, e.status, e.manager_display,
            e.manager_mail, e.location or "", e.note or "",
            st.next_inspect, st.days_left, st.replace_due, GRADES[st.grade]["label"], st.reason,
        ]


def to_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS + EXPORT_EXTRA)
    for vals in export_values(rows):
        w.writerow(["" if v is None else (v.isoformat() if isinstance(v, date) else v) for v in vals])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # UTF-8 BOM (엑셀 한글 깨짐 방지)


def to_xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "장비대장"
    head = COLUMNS + EXPORT_EXTRA
    ws.append(head)
    fill = PatternFill("solid", fgColor="E8EEF8")
    for c in ws[1]:
        c.font = Font(bold=True, color="14284B")
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
    for vals in export_values(rows):
        ws.append(vals)
    date_cols = [head.index(n) + 1 for n in ("지급일", "최종검사일", "차기검사일", "교체예정일")]
    for row in ws.iter_rows(min_row=2):
        for ci in date_cols:
            row[ci - 1].number_format = "yyyy-mm-dd"
    widths = [14, 26, 20, 20, 14, 12, 12, 10, 8, 10, 26, 14, 24, 12, 9, 12, 9, 14]
    for i, wdt in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = wdt
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template_csv(office_name, type_name) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["# 작성 안내: 사업소·장비구분은 이름 또는 코드로 입력, 날짜는 YYYY-MM-DD, 필수: 사업소/장비구분/최종검사일. "
                "관리번호를 비워 두면 자동 생성됩니다. 이 줄은 지워도 됩니다."])
    w.writerow(COLUMNS)
    w.writerow(["", office_name, type_name, "고압용 7,000V급", "SN-0001", "2025-03-02", "2026-05-15", "합격", "사용중",
                "홍길동", "gildong.hong@example.com", "보호구함 1", "관리번호는 비워 두면 자동 생성"])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def template_xlsx(office_name, type_name) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "등록양식"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["", office_name, type_name, "고압용 7,000V급", "SN-0001", "2025-03-02", "2026-05-15", "합격", "사용중",
               "홍길동", "gildong.hong@example.com", "보호구함 1", "관리번호는 비워 두면 자동 생성"])
    for i in range(1, len(COLUMNS) + 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
