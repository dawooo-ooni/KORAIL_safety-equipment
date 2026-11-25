"""공용 도우미: 권한 데코레이터, 날짜 해석, 입력 검증."""
import re
from datetime import date, datetime, timedelta
from functools import wraps

from flask import abort, request
from flask_login import current_user, login_required

LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{3,20}$")
CODE_RE = re.compile(r"^[A-Z0-9]{1,6}$")
MGMT_NO_RE = re.compile(r"^[A-Z0-9]{1,6}-[A-Z0-9]{1,6}-[A-Z0-9]{1,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def admin_required(view):
    """로그인 + 관리자 권한. 권한이 없으면 403."""
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if not current_user.is_admin:
            abort(403)
        return view(*a, **kw)
    return wrapped


_DATE_RE = re.compile(r"^\s*(\d{4})[-./ ]?(\d{1,2})[-./ ]?(\d{1,2})")


def parse_date(v):
    """YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD, YYYYMMDD, datetime/date, 엑셀 날짜 일련번호를 해석.
    해석할 수 없으면 None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        if 20000 < n < 80000:  # 엑셀 일련번호 (1954~2119년)
            return date(1899, 12, 30) + timedelta(days=int(n))
        v = str(int(n))
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", s):
        return parse_date(float(s))
    m = _DATE_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")[:45]


def clean(v, maxlen=None):
    s = (v or "").strip()
    return s[:maxlen] if maxlen else s


def password_problem(pw, old_hash_user=None):
    """비밀번호 규칙 위반 사유 (없으면 None). 6자 이상, 이전과 같은 비밀번호 금지."""
    if len(pw or "") < 6:
        return "비밀번호는 6자 이상이어야 합니다."
    if old_hash_user is not None and old_hash_user.check_password(pw):
        return "기존 비밀번호와 다른 비밀번호를 정하세요."
    return None
