"""감사 로그 기록 (작업지시서 8.6). 호출한 쪽의 트랜잭션에 함께 커밋된다."""
import json

from flask import has_request_context
from flask_login import current_user

from ..extensions import db
from ..models import AuditLog
from ..utils import client_ip


def _dump(v):
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def log(action, target_type=None, target_id=None, before=None, after=None, user_id=None):
    if user_id is None and has_request_context():
        try:
            if current_user.is_authenticated:
                user_id = current_user.id
        except Exception:  # noqa: BLE001
            user_id = None
    entry = AuditLog(
        user_id=user_id, action=action, target_type=target_type, target_id=target_id,
        before_json=_dump(before), after_json=_dump(after),
        ip=client_ip() if has_request_context() else None,
    )
    db.session.add(entry)
    return entry


def diff(before: dict, after: dict):
    """바뀐 키만 남긴 (before, after)."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}
