"""장비 대장 공용 로직: 상태 계산 묶음, 필터/정렬, 관리번호 생성, 검사 반영."""
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from ..extensions import db
from ..models import Equipment, EquipmentType, Inspection, Office
from . import settings as cfg
from .calc import GRADE_ORDER, evaluate

# 장비구분 예시 그림 (static/img/types/)
ICON_BY_CODE = {
    "GLV": "glove.svg", "SHO": "shoe.svg", "BOT": "boot.svg", "HLM": "helmet.svg", "SLV": "sleeve.svg",
    "DLV": "detector_low.svg", "DHV": "detector_high.svg", "DSH": "detector_xhigh.svg",
    "GND": "ground.svg", "DSC": "discharge.svg", "HST": "stick.svg", "HAR": "harness.svg",
}
_ICON_BY_NAME = [
    ("장갑", "glove.svg"), ("장화", "boot.svg"), ("절연화", "shoe.svg"), ("안전모", "helmet.svg"),
    ("검전", "detector.svg"), ("접지", "ground.svg"), ("방전", "discharge.svg"), ("봉", "stick.svg"),
    ("안전대", "harness.svg"), ("그네", "harness.svg"), ("복", "sleeve.svg"), ("소매", "sleeve.svg"),
    ("화", "shoe.svg"), ("모", "helmet.svg"),
]


def icon_for(etype):
    if etype is None:
        return "default.svg"
    if etype.icon:
        return etype.icon
    if etype.code in ICON_BY_CODE:
        return ICON_BY_CODE[etype.code]
    for word, f in _ICON_BY_NAME:
        if word in (etype.name or ""):
            return f
    return "default.svg"


def status_of(eq, ref=None):
    u, w = cfg.thresholds()
    return evaluate(eq, eq.etype, ref or date.today(), u, w)


class Row:
    """템플릿용: 장비 + 계산 결과"""
    __slots__ = ("eq", "st")

    def __init__(self, eq, st):
        self.eq, self.st = eq, st


def all_rows(ref=None, query=None):
    q = query if query is not None else Equipment.query
    # 목록 화면에서 장비마다 따로 질의하지 않도록(N+1) 관계를 한 번에 불러온다
    q = q.options(joinedload(Equipment.office), joinedload(Equipment.etype), joinedload(Equipment.manager_user),
                  selectinload(Equipment.photos))
    ref = ref or date.today()
    u, w = cfg.thresholds()
    return [Row(eq, evaluate(eq, eq.etype, ref, u, w)) for eq in q.all()]


SORT_KEYS = ("grade", "mgmt_no", "office", "type", "model", "last_inspect", "result",
             "next", "eff", "repl", "status", "manager")


def filter_rows(rows, args):
    """args: q, office, type, grade, status (request.args 형태)"""
    q = (args.get("q") or "").strip().lower()
    office = args.get("office") or ""
    etype = args.get("type") or ""
    grade = args.get("grade") or ""
    status = args.get("status") or ""
    out = []
    for r in rows:
        e = r.eq
        if office and e.office.code != office:
            continue
        if etype and e.etype.code != etype:
            continue
        if grade and r.st.grade != grade:
            continue
        if status and e.status != status:
            continue
        if q and not any(q in str(v or "").lower() for v in
                         (e.mgmt_no, e.model, e.manager_display, e.serial_no, e.location)):
            continue
        out.append(r)
    return out


def sort_rows(rows, key="eff", desc=False):
    far = date(9999, 12, 31)
    keyf = {
        "grade": lambda r: (GRADE_ORDER[r.st.grade], r.st.effective_days),
        "mgmt_no": lambda r: r.eq.mgmt_no,
        "office": lambda r: r.eq.office.name,
        "type": lambda r: r.eq.etype.name,
        "model": lambda r: r.eq.model or "",
        "last_inspect": lambda r: r.eq.last_inspect_date,
        "result": lambda r: r.eq.last_result,
        "next": lambda r: r.st.next_inspect,
        "eff": lambda r: r.st.effective_days,
        "repl": lambda r: r.st.replace_due or far,
        "status": lambda r: r.eq.status,
        "manager": lambda r: r.eq.manager_display,
    }.get(key, lambda r: r.st.effective_days)
    return sorted(rows, key=lambda r: (keyf(r), r.eq.mgmt_no), reverse=desc)


def grade_counts(rows):
    cnt = {"over": 0, "urgent": 0, "warn": 0, "ok": 0, "unusable": 0}
    for r in rows:
        cnt[r.st.grade] += 1
    return cnt


# ---------------- 관리번호 ----------------

def next_mgmt_no(office_code, type_code, exclude_id=None):
    """{사업소코드}-{구분코드}-{3자리 순번}. 같은 접두어 안에서 최대 순번 + 1."""
    prefix = f"{office_code}-{type_code}-"
    q = db.session.query(Equipment.id, Equipment.mgmt_no).filter(Equipment.mgmt_no.like(prefix + "%"))
    mx = 0
    for eid, no in q:
        if exclude_id and eid == exclude_id:
            continue
        tail = no[len(prefix):]
        if tail.isdigit():
            mx = max(mx, int(tail))
    return f"{prefix}{mx + 1:03d}"


def save_with_new_no(eq, office_code, type_code, attempts=5):
    """관리번호를 만들어 저장(flush). UNIQUE 충돌 시 다시 시도.
    호출자가 commit 한다. SAVEPOINT 를 써서 충돌 시 해당 시도만 되돌린다."""
    last_err = None
    for _ in range(attempts):
        eq.mgmt_no = next_mgmt_no(office_code, type_code, exclude_id=eq.id)
        sp = db.session.begin_nested()
        try:
            db.session.add(eq)
            db.session.flush()
            sp.commit()
            return eq
        except IntegrityError as e:  # 동시에 같은 번호를 쓴 경우
            sp.rollback()
            last_err = e
    raise last_err


# ---------------- 검사 반영 ----------------

def sync_latest_inspection(eq):
    """검사 이력 중 가장 최근 것으로 최종검사일·결과를 맞춘다."""
    insp = (Inspection.query.filter_by(equipment_id=eq.id)
            .order_by(Inspection.inspect_date.desc(), Inspection.id.desc()).first())
    if insp:
        eq.last_inspect_date = insp.inspect_date
        eq.last_result = insp.result


def lookups():
    offices = Office.query.order_by(Office.code).all()
    types = EquipmentType.query.order_by(EquipmentType.sort_order, EquipmentType.code).all()
    return offices, types


def find_office(v, include_inactive=True):
    v = (v or "").strip()
    if not v:
        return None
    q = Office.query
    o = q.filter(Office.code == v.upper()).first() or q.filter(Office.name == v).first()
    if o and (include_inactive or o.is_active):
        return o
    return None


def find_type(v, include_inactive=True):
    v = (v or "").strip()
    if not v:
        return None
    q = EquipmentType.query
    t = q.filter(EquipmentType.code == v.upper()).first() or q.filter(EquipmentType.name == v).first()
    if t and (include_inactive or t.is_active):
        return t
    return None
