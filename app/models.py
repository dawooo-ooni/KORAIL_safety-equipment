"""SQLAlchemy 모델 (작업지시서 5장 DDL과 같은 스키마)."""
from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index, func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

NOW = datetime.now
TS = func.current_timestamp()

RESULTS = ("합격", "불합격")
STATUSES = ("사용중", "보관", "수리중", "폐기")
ROLES = ("user", "admin")
USER_STATUSES = ("pending", "active", "rejected", "suspended")
VERDICTS = ("적합", "만료", "사용불가", "미등록")


class Office(db.Model):
    __tablename__ = "offices"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(6), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    created_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)

    def to_dict(self):
        return {"code": self.code, "name": self.name, "is_active": self.is_active}


class EquipmentType(db.Model):
    __tablename__ = "equipment_types"
    __table_args__ = (
        CheckConstraint("inspect_months BETWEEN 1 AND 120", name="ck_type_inspect"),
        CheckConstraint("replace_months IS NULL OR replace_months BETWEEN 1 AND 600", name="ck_type_replace"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(6), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    inspect_months = db.Column(db.Integer, nullable=False)
    replace_months = db.Column(db.Integer)
    icon = db.Column(db.String(30))
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default="1")

    def to_dict(self):
        return {
            "code": self.code, "name": self.name, "inspect_months": self.inspect_months,
            "replace_months": self.replace_months, "icon": self.icon,
            "sort_order": self.sort_order, "is_active": self.is_active,
        }


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user','admin')", name="ck_user_role"),
        CheckConstraint("status IN ('pending','active','rejected','suspended')", name="ck_user_status"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # SQLite 에서는 DDL대로 COLLATE NOCASE, PostgreSQL 에서는 일반 문자열 (조회는 앱에서 소문자 비교)
    login_id = db.Column(db.String(20).with_variant(db.String(20, collation="NOCASE"), "sqlite"),
                         nullable=False, unique=True)
    name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    office_id = db.Column(db.Integer, db.ForeignKey("offices.id"))
    role = db.Column(db.String(10), nullable=False, default="user", server_default="user")
    status = db.Column(db.String(10), nullable=False, default="pending", server_default="pending")
    password_hash = db.Column(db.String(255), nullable=False)
    must_change_pw = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    signup_memo = db.Column(db.String(200))
    reject_reason = db.Column(db.String(200))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    failed_logins = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)

    office = db.relationship("Office", foreign_keys=[office_id])
    approver = db.relationship("User", remote_side=[id], foreign_keys=[approved_by])

    # Flask-Login: 활성 계정만 로그인 상태로 인정
    @property
    def is_active(self):
        return self.status == "active"

    @property
    def is_admin(self):
        return self.role == "admin" and self.status == "active"

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return bool(self.password_hash) and check_password_hash(self.password_hash, pw)

    def to_audit(self):
        return {
            "login_id": self.login_id, "name": self.name, "email": self.email,
            "office": self.office.code if self.office else None, "role": self.role,
            "status": self.status, "must_change_pw": self.must_change_pw,
        }


class Equipment(db.Model):
    __tablename__ = "equipment"
    __table_args__ = (
        CheckConstraint("last_result IN ('합격','불합격')", name="ck_eq_result"),
        CheckConstraint("status IN ('사용중','보관','수리중','폐기')", name="ck_eq_status"),
        Index("ix_equipment_office", "office_id"),
        Index("ix_equipment_type", "type_id"),
        Index("ix_equipment_status", "status"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mgmt_no = db.Column(db.String(30), nullable=False, unique=True)
    office_id = db.Column(db.Integer, db.ForeignKey("offices.id"), nullable=False)
    type_id = db.Column(db.Integer, db.ForeignKey("equipment_types.id"), nullable=False)
    model = db.Column(db.String(100))
    serial_no = db.Column(db.String(100))
    issue_date = db.Column(db.Date)
    last_inspect_date = db.Column(db.Date, nullable=False)
    last_result = db.Column(db.String(10), nullable=False, default="합격", server_default="합격")
    status = db.Column(db.String(10), nullable=False, default="사용중", server_default="사용중")
    manager_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    manager_name = db.Column(db.String(50))
    manager_email = db.Column(db.String(120))
    location = db.Column(db.String(100))
    note = db.Column(db.String(300))
    is_sample = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)
    updated_at = db.Column(db.DateTime, nullable=False, default=NOW, onupdate=NOW, server_default=TS)

    # 낙관적 잠금: UPDATE ... WHERE version=? 으로 저장하고, 다르면 StaleDataError
    __mapper_args__ = {"version_id_col": version}

    office = db.relationship("Office")
    etype = db.relationship("EquipmentType")
    manager_user = db.relationship("User", foreign_keys=[manager_user_id])
    updater = db.relationship("User", foreign_keys=[updated_by])
    inspections = db.relationship(
        "Inspection", backref="equipment", cascade="all, delete-orphan", passive_deletes=True,
        order_by="desc(Inspection.inspect_date), desc(Inspection.id)",
    )
    photos = db.relationship(
        "Photo", backref="equipment", cascade="all, delete-orphan", passive_deletes=True,
        order_by="desc(Photo.uploaded_at)",
    )

    @property
    def manager_display(self):
        if self.manager_user:
            return self.manager_user.name
        return self.manager_name or ""

    @property
    def manager_mail(self):
        if self.manager_user and self.manager_user.email:
            return self.manager_user.email
        return self.manager_email or ""

    @property
    def primary_photo(self):
        for p in self.photos:
            if p.is_primary:
                return p
        return self.photos[0] if self.photos else None

    def to_audit(self):
        d = lambda v: v.isoformat() if v else None  # noqa: E731
        return {
            "mgmt_no": self.mgmt_no,
            "office": self.office.code if self.office else self.office_id,
            "type": self.etype.code if self.etype else self.type_id,
            "model": self.model, "serial_no": self.serial_no,
            "issue_date": d(self.issue_date), "last_inspect_date": d(self.last_inspect_date),
            "last_result": self.last_result, "status": self.status,
            "manager_user_id": self.manager_user_id, "manager_name": self.manager_name,
            "manager_email": self.manager_email, "location": self.location, "note": self.note,
        }


class Inspection(db.Model):
    __tablename__ = "inspections"
    __table_args__ = (
        CheckConstraint("result IN ('합격','불합격')", name="ck_insp_result"),
        Index("ix_inspections_equipment", "equipment_id", "inspect_date"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False)
    inspect_date = db.Column(db.Date, nullable=False)
    result = db.Column(db.String(10), nullable=False)
    inspector = db.Column(db.String(100))
    memo = db.Column(db.String(300))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)

    creator = db.relationship("User", foreign_keys=[created_by])


class Photo(db.Model):
    __tablename__ = "photos"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False)
    file_path = db.Column(db.String(255), nullable=False)
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    size_bytes = db.Column(db.Integer)
    is_primary = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)


class WorkCheck(db.Model):
    __tablename__ = "work_checks"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    work_date = db.Column(db.Date, nullable=False)
    work_title = db.Column(db.String(200))
    checked_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    all_ok = db.Column(db.Boolean, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)

    checker = db.relationship("User")
    items = db.relationship("WorkCheckItem", backref="work_check", cascade="all, delete-orphan",
                            passive_deletes=True, order_by="WorkCheckItem.id")


class WorkCheckItem(db.Model):
    __tablename__ = "work_check_items"
    __table_args__ = (
        CheckConstraint("verdict IN ('적합','만료','사용불가','미등록')", name="ck_wci_verdict"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    work_check_id = db.Column(db.Integer, db.ForeignKey("work_checks.id", ondelete="CASCADE"), nullable=False)
    mgmt_no = db.Column(db.String(30), nullable=False)
    # 장비가 나중에 삭제돼도 점검 기록은 남도록 SET NULL
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="SET NULL"))
    verdict = db.Column(db.String(10), nullable=False)
    reason = db.Column(db.String(200))
    next_inspect_date = db.Column(db.Date)

    equipment = db.relationship("Equipment")


class NotificationLog(db.Model):
    __tablename__ = "notification_logs"
    __table_args__ = (
        CheckConstraint("trigger IN ('auto','manual')", name="ck_nl_trigger"),
        CheckConstraint("status IN ('sent','failed','skipped')", name="ck_nl_status"),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sent_at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)
    trigger = db.Column(db.String(10), nullable=False)
    recipient = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    item_count = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), nullable=False)
    error = db.Column(db.String(500))
    sent_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    sender = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    at = db.Column(db.DateTime, nullable=False, default=NOW, server_default=TS)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(30), nullable=False)
    target_type = db.Column(db.String(30))
    target_id = db.Column(db.Integer)
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    ip = db.Column(db.String(45))

    user = db.relationship("User")


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False)
