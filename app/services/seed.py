"""초기 데이터 (작업지시서 5.4). 여러 번 실행해도 중복되지 않는다."""
from datetime import date, datetime, timedelta

from flask import current_app

from ..extensions import db
from ..models import Equipment, EquipmentType, Inspection, Office, User, WorkCheck
from . import settings as cfg
from .calc import add_months
from .ledger import ICON_BY_CODE

OFFICES = [
    ("SEL", "서울고속전기사업소(샘플)"),
    ("DJN", "대전고속전기사업소(샘플)"),
    ("BSN", "부산고속전기사업소(샘플)"),
]
TYPES = [
    ("GLV", "절연장갑"), ("SHO", "절연화"), ("BOT", "절연장화"), ("HLM", "절연안전모"),
    ("SLV", "절연복·절연소매"), ("DLV", "검전기(저압)"), ("DHV", "검전기(고압)"), ("DSH", "검전기(특고압)"),
    ("GND", "단락접지기구"), ("DSC", "방전봉"), ("HST", "절연봉(활선작업용 기구)"), ("HAR", "안전대(그네식)"),
]
# (login, name, role, office, status, memo, email)
SAMPLE_USERS = [
    ("manager01", "이서연", "admin", "DJN", "active", None, "seoyeon.lee@example.com"),
    ("user01", "김민수", "user", "SEL", "active", None, "minsu.kim@example.com"),
    ("user02", "최유진", "user", "BSN", "active", None, "yujin.choi@example.com"),
    ("user03", "박지훈", "user", "DJN", "pending", "내선 2345, 전기설비팀 (샘플 신청)", None),
    ("user04", "정태호", "user", "SEL", "pending", "신규 전입자 (샘플 신청)", None),
]
SAMPLE_LOGINS = tuple(u[0] for u in SAMPLE_USERS)
SAMPLE_PASSWORD = "1111"

_MANAGERS = {
    "SEL": [("김민수", "minsu.kim@example.com"), ("이서연", "seoyeon.lee@example.com")],
    "DJN": [("박지훈", "jihoon.park@example.com"), ("최유진", "yujin.choi@example.com")],
    "BSN": [("정태호", "taeho.jung@example.com"), ("윤성민", "sungmin.yoon@example.com")],
}
_MODELS = {
    "GLV": "고압용 7,000V급", "SHO": "전기용 절연화 표준형", "BOT": "절연장화 고압용", "HLM": "AE형 절연안전모",
    "SLV": "절연소매 고압용", "DLV": "저압 검전기 600V", "DHV": "고압 검전기 7kV", "DSH": "특고압 검전기 25kV",
    "GND": "단락접지기구 3상", "DSC": "방전봉 25kV", "HST": "절연봉 3단", "HAR": "그네식 안전대",
}
_LOCS = ["자재창고 A", "보호구함 1", "보호구함 2", "작업차량 1호", "작업차량 2호", "검수고 공구실"]


def seed_base(out=print):
    cfg.ensure_defaults()
    for code, name in OFFICES:
        if not Office.query.filter_by(code=code).first():
            db.session.add(Office(code=code, name=name))
    for i, (code, name) in enumerate(TYPES):
        if not EquipmentType.query.filter_by(code=code).first():
            db.session.add(EquipmentType(code=code, name=name, inspect_months=6, replace_months=None,
                                         icon=ICON_BY_CODE.get(code), sort_order=i))
    if not User.query.filter(User.login_id == "admin").first():
        u = User(login_id="admin", name="관리자", role="admin", status="active", must_change_pw=True)
        u.set_password(current_app.config["ADMIN_INIT_PASSWORD"])
        db.session.add(u)
        out("관리자 계정 admin 을 만들었습니다. (초기 비밀번호: .env 의 ADMIN_INIT_PASSWORD, 첫 로그인 때 변경)")
    db.session.commit()
    out(f"기본 데이터 확인 완료: 사업소 {Office.query.count()}개, 장비구분 {EquipmentType.query.count()}개")


class _Rng:
    """시제품과 같은 결정적 난수 (mulberry32)"""

    def __init__(self, seed):
        self.a = seed & 0xFFFFFFFF

    def __call__(self):
        self.a = (self.a + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.a
        t = ((t ^ (t >> 15)) * (1 | t)) & 0xFFFFFFFF
        t = (t + (((t ^ (t >> 7)) * (61 | t)) & 0xFFFFFFFF) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    def ri(self, lo, hi):
        return lo + int(self() * (hi - lo + 1))


def seed_sample(out=print, with_users=True):
    seed_base(out=lambda *_: None)
    offices = {o.code: o for o in Office.query.all()}
    admin = User.query.filter(User.login_id == "admin").first()
    # 체험용 예시 계정 (인터넷 공개 서버에서는 with_users=False 권장)
    n_users = 0
    for login, name, role, office, status, memo, email in (SAMPLE_USERS if with_users else []):
        if User.query.filter(User.login_id == login).first():
            continue
        u = User(login_id=login, name=name, role=role, status=status, office_id=offices[office].id,
                 signup_memo=memo, email=email)
        if status == "active":
            u.approved_by = admin.id if admin else None
            u.approved_at = datetime.now()
        u.set_password(SAMPLE_PASSWORD)
        db.session.add(u)
        n_users += 1
    # 샘플 장비 50건 (이미 있으면 건너뜀)
    if Equipment.query.filter_by(is_sample=True).count():
        db.session.commit()
        out(f"샘플 장비가 이미 있어 건너뜁니다. (예시 계정 {n_users}개 추가)")
        return
    types = {t.code: t for t in EquipmentType.query.all()}
    type_codes = [c for c, _ in TYPES if c in types]
    rnd = _Rng(20261125)
    today = date.today()
    seq = {}
    # 모든 등급이 나오도록 앞쪽 몇 건은 구간을 고정
    forced = {0: "over", 1: "urgent", 2: "warn", 3: "ok", 4: "over", 5: "urgent", 6: "warn"}
    for i in range(50):
        oc = OFFICES[i % 3][0]
        tc = type_codes[rnd.ri(0, len(type_codes) - 1)]
        etype = types[tc]
        r = rnd()
        band = forced.get(i) or ("over" if r < 0.12 else "urgent" if r < 0.22 else "warn" if r < 0.40 else "ok")
        nxt_target = {
            "over": -rnd.ri(4, 33), "urgent": rnd.ri(0, 6), "warn": rnd.ri(9, 29), "ok": rnd.ri(32, 178),
        }[band]
        # 차기검사일이 오늘 + nxt_target 이 되도록 최종검사일을 역산
        nxt = today + timedelta(days=nxt_target)
        last = add_months(nxt, -etype.inspect_months)
        key = f"{oc}-{tc}"
        seq[key] = seq.get(key, 0) + 1
        no = f"{key}-{seq[key]:03d}"
        while Equipment.query.filter_by(mgmt_no=no).first():
            seq[key] += 1
            no = f"{key}-{seq[key]:03d}"
        mname, memail = _MANAGERS[oc][i % 2]
        fail = i in (7, 23)
        status = "폐기" if i == 11 else "수리중" if i == 30 else "보관" if i % 9 == 0 else "사용중"
        res = "불합격" if fail else "합격"
        eq = Equipment(
            mgmt_no=no, office_id=offices[oc].id, type_id=etype.id, model=_MODELS.get(tc), serial_no=f"SN{24000 + i * 37}",
            issue_date=last - timedelta(days=rnd.ri(365, 1095)), last_inspect_date=last, last_result=res, status=status,
            manager_name=mname, manager_email=memail, location=_LOCS[rnd.ri(0, len(_LOCS) - 1)], is_sample=True,
            created_by=admin.id if admin else None, updated_by=admin.id if admin else None,
        )
        db.session.add(eq)
        db.session.flush()
        db.session.add(Inspection(equipment_id=eq.id, inspect_date=add_months(last, -etype.inspect_months), result="합격",
                                  inspector="외부 시험기관"))
        db.session.add(Inspection(equipment_id=eq.id, inspect_date=last, result=res, inspector="외부 시험기관",
                                  memo="내전압 시험 불합격" if fail else None))
    db.session.commit()
    out(f"샘플 장비 50건과 예시 계정 {n_users}개를 넣었습니다. (예시 계정 비밀번호: {SAMPLE_PASSWORD})")


def detach_user(user):
    """사용자를 지우기 전에 다른 테이블의 참조를 끊는다."""
    from ..models import AuditLog, Inspection as Insp, NotificationLog, Photo
    uid = user.id
    Equipment.query.filter_by(manager_user_id=uid).update(
        {"manager_user_id": None, "manager_name": user.name, "manager_email": user.email}, synchronize_session=False)
    Equipment.query.filter_by(created_by=uid).update({"created_by": None}, synchronize_session=False)
    Equipment.query.filter_by(updated_by=uid).update({"updated_by": None}, synchronize_session=False)
    Insp.query.filter_by(created_by=uid).update({"created_by": None}, synchronize_session=False)
    Photo.query.filter_by(uploaded_by=uid).update({"uploaded_by": None}, synchronize_session=False)
    NotificationLog.query.filter_by(sent_by=uid).update({"sent_by": None}, synchronize_session=False)
    AuditLog.query.filter_by(user_id=uid).update({"user_id": None}, synchronize_session=False)
    User.query.filter_by(approved_by=uid).update({"approved_by": None}, synchronize_session=False)


def delete_samples(current_user_id=None):
    """is_sample=1 장비와 예시 계정만 지운다. 반환: (장비 수, 계정 수)"""
    from .photos import remove_file
    n_eq = 0
    for eq in Equipment.query.filter_by(is_sample=True).all():
        for p in eq.photos:
            remove_file(p.file_path)
        db.session.delete(eq)
        n_eq += 1
    n_u = 0
    for u in User.query.filter(User.login_id.in_(SAMPLE_LOGINS)).all():
        if u.id == current_user_id:
            continue
        for wc in WorkCheck.query.filter_by(checked_by=u.id).all():
            db.session.delete(wc)
        detach_user(u)
        db.session.delete(u)
        n_u += 1
    return n_eq, n_u
