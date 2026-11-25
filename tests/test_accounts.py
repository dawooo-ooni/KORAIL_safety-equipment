"""11장 수용 기준 — 계정·권한"""
from app.extensions import db
from app.models import AuditLog, User

from .conftest import ADMIN_PW, login


def _signup(client, login_id="newbie", pw="abc12345"):
    return client.post("/signup", data={"login_id": login_id, "name": "신입", "office_id": 1, "email": "n@example.com",
                                        "password": pw, "password2": pw, "memo": "테스트"})


def test_signup_approve_flow(app, client, admin_client):
    c = app.test_client()
    r = _signup(c)
    assert r.status_code == 200 and "가입 신청 완료" in r.get_data(as_text=True)
    # 승인 전 로그인 불가
    r = login(c, "newbie", "abc12345")
    assert r.status_code == 200 and "승인되지 않았습니다" in r.get_data(as_text=True)
    with app.app_context():
        uid = User.query.filter_by(login_id="newbie").first().id
    r = admin_client.post(f"/admin/users/{uid}/approve", data={"role": "user"})
    assert r.status_code == 302
    r = login(c, "newbie", "abc12345")
    assert r.status_code == 302


def test_reject_shows_reason_and_resignup(app, client, admin_client):
    c = app.test_client()
    _signup(c, "rejectme")
    with app.app_context():
        uid = User.query.filter_by(login_id="rejectme").first().id
    admin_client.post(f"/admin/users/{uid}/reject", data={"reason": "소속 확인 불가"})
    r = login(c, "rejectme", "abc12345")
    body = r.get_data(as_text=True)
    assert "반려" in body and "소속 확인 불가" in body
    # 다시 신청 가능
    r = _signup(c, "rejectme", "xyz98765")
    assert "가입 신청 완료" in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(login_id="rejectme").first().status == "pending"


def test_user_cannot_access_admin_urls(user_client):
    for url in ("/admin/users", "/admin/types", "/admin/settings", "/notify", "/labels", "/equipment/new",
                "/admin/backup", "/admin/audit", "/admin/export.csv"):
        assert user_client.get(url).status_code == 403, url
    assert user_client.post("/equipment/1/delete", data={"confirm_no": "x"}).status_code == 403
    assert user_client.post("/admin/users/1/role", data={"role": "user"}).status_code == 403


def test_anonymous_redirected_to_login(client):
    r = client.get("/equipment")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_lock_after_5_failures(app, client):
    for _ in range(5):
        login(client, "user02", "wrong")
    r = login(client, "user02", "1111")  # 맞는 비밀번호여도 잠김
    assert r.status_code == 200 and "잠" in r.get_data(as_text=True)
    with app.app_context():
        u = User.query.filter_by(login_id="user02").first()
        assert u.locked_until is not None
        assert AuditLog.query.filter_by(action="login_fail").count() >= 5


def test_lock_counts_exactly_5_and_shows_remaining(app, client):
    r = login(client, "user02", "wrong")
    assert "남은 시도 4회" in r.get_data(as_text=True)
    for _ in range(3):
        login(client, "user02", "wrong")
    with app.app_context():
        assert User.query.filter_by(login_id="user02").first().failed_logins == 4
    r = login(client, "user02", "wrong")  # 5번째에 잠김
    assert "잠겼습니다" in r.get_data(as_text=True)
    with app.app_context():
        u = User.query.filter_by(login_id="user02").first()
        assert u.locked_until is not None and u.failed_logins == 0


def test_login_tolerates_pasted_whitespace(client):
    r = login(client, "user01", " 1111\r\n")
    assert r.status_code == 302


def test_last_admin_cannot_be_demoted_or_deleted(app, admin_client):
    with app.app_context():
        # 예시 관리자 manager01 을 먼저 강등 → admin 만 남음
        m = User.query.filter_by(login_id="manager01").first()
        admin = User.query.filter_by(login_id="admin").first()
        mid, aid = m.id, admin.id
    admin_client.post(f"/admin/users/{mid}/role", data={"role": "user"})
    admin_client.post(f"/admin/users/{aid}/role", data={"role": "user"})
    admin_client.post(f"/admin/users/{aid}/delete")
    with app.app_context():
        a = db.session.get(User, aid)
        assert a is not None and a.role == "admin"
        assert db.session.get(User, mid).role == "user"


def test_must_change_password_forced(app, client):
    with app.app_context():
        a = User.query.filter_by(login_id="admin").first()
        a.must_change_pw = True
        db.session.commit()
    login(client, "admin", ADMIN_PW)
    r = client.get("/")
    assert r.status_code == 302 and "/account/password" in r.headers["Location"]
    # 같은 비밀번호로 변경 금지
    r = client.post("/account/password", data={"password": ADMIN_PW, "password2": ADMIN_PW})
    assert "기존 비밀번호와 다른" in r.get_data(as_text=True)
    r = client.post("/account/password", data={"password": "NewPass123", "password2": "NewPass123"})
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_reset_password_shows_temp_once(app, admin_client):
    with app.app_context():
        uid = User.query.filter_by(login_id="user01").first().id
    r = admin_client.post(f"/admin/users/{uid}/reset-password", follow_redirects=True)
    assert "임시 비밀번호:" in r.get_data(as_text=True)
    r = admin_client.get("/admin/users")
    assert "임시 비밀번호:" not in r.get_data(as_text=True)  # 1회만 표시
    with app.app_context():
        assert User.query.filter_by(login_id="user01").first().must_change_pw
