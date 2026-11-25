"""settings 테이블(키-값) 읽기/쓰기. 요청마다 한 번만 읽도록 g 에 캐시."""
from flask import g, has_request_context

from ..extensions import db
from ..models import Setting

DEFAULTS = {
    "urgent_days": "7",
    "warn_days": "30",
    "allow_signup": "1",
    "alert_enabled": "0",
    "alert_time": "08:00",
    "alert_cc": "",
    "logo_path": "",
}


def _all():
    if has_request_context() and "_settings" in g:
        return g._settings
    data = dict(DEFAULTS)
    for s in Setting.query.filter(~Setting.key.like("imp\\_%", escape="\\")).all():  # 가져오기 미리보기 제외
        data[s.key] = s.value
    if has_request_context():
        g._settings = data
    return data


def get(key):
    return _all().get(key, DEFAULTS.get(key, ""))


def get_int(key, default=0):
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return default


def get_bool(key):
    return str(get(key)).strip() in ("1", "true", "on", "yes")


def thresholds():
    return get_int("urgent_days", 7), get_int("warn_days", 30)


def set(key, value):  # noqa: A001 - 모듈 함수명
    value = "" if value is None else str(value)
    row = db.session.get(Setting, key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    if has_request_context() and "_settings" in g:
        g._settings[key] = value


def ensure_defaults():
    for k, v in DEFAULTS.items():
        if not db.session.get(Setting, k):
            db.session.add(Setting(key=k, value=v))
