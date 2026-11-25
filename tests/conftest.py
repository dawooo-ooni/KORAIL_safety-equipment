import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import User  # noqa: E402
from app.services.seed import seed_base, seed_sample  # noqa: E402

ADMIN_PW = "Admin!2026"


@pytest.fixture
def app(tmp_path):
    app = create_app("testing", {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
    })
    # 업로드·백업이 실제 instance 폴더를 건드리지 않도록 임시 폴더 사용
    app.instance_path = str(tmp_path / "instance")
    for sub in ("uploads/photos", "backups", "tmp"):
        os.makedirs(os.path.join(app.instance_path, sub), exist_ok=True)
    with app.app_context():
        _db.create_all()
        seed_base(out=lambda *_: None)
        seed_sample(out=lambda *_: None)
        # 테스트 편의: admin 초기 비밀번호 변경 강제 해제
        a = User.query.filter_by(login_id="admin").first()
        a.must_change_pw = False
        a.email = "admin@example.com"
        _db.session.commit()
    yield app
    with app.app_context():
        _db.session.remove()
        _db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, login_id, pw):
    return client.post("/login", data={"login_id": login_id, "password": pw}, follow_redirects=False)


@pytest.fixture
def admin_client(client):
    r = login(client, "admin", ADMIN_PW)
    assert r.status_code == 302
    return client


@pytest.fixture
def user_client(app):
    c = app.test_client()
    r = login(c, "user01", "1111")
    assert r.status_code == 302
    return c
