"""11장 수용 기준 — 장비 대장, 사진, QR, 작업 전 점검, 감사 로그"""
import io
from datetime import date, timedelta

from PIL import Image

from app.extensions import db
from app.models import AuditLog, Equipment, EquipmentType, Office, Photo, WorkCheck
from app.services.calc import add_months
from app.services.ledger import status_of


def _ids(app):
    with app.app_context():
        return Office.query.filter_by(code="SEL").first().id, EquipmentType.query.filter_by(code="GLV").first().id


def test_register_with_required_only(app, admin_client):
    oid, tid = _ids(app)
    r = admin_client.post("/equipment/new", data={"office_id": oid, "type_id": tid,
                                                  "last_inspect_date": (date.today() - timedelta(days=10)).isoformat()})
    assert r.status_code == 302
    with app.app_context():
        eq = Equipment.query.order_by(Equipment.id.desc()).first()
        assert eq.mgmt_no.startswith("SEL-GLV-")
        assert len(eq.inspections) == 1
        assert AuditLog.query.filter_by(action="create", target_type="equipment", target_id=eq.id).count() == 1


def test_register_validation(app, admin_client):
    oid, tid = _ids(app)
    r = admin_client.post("/equipment/new", data={"office_id": oid, "type_id": tid,
                                                  "last_inspect_date": (date.today() + timedelta(days=1)).isoformat()})
    assert r.status_code == 400 and "오늘보다 늦습니다" in r.get_data(as_text=True)
    r = admin_client.post("/equipment/new", data={"office_id": oid, "last_inspect_date": "2026-13-40"})
    assert r.status_code == 400


def test_mgmt_no_increments(app, admin_client):
    oid, tid = _ids(app)
    d = (date.today() - timedelta(days=5)).isoformat()
    admin_client.post("/equipment/new", data={"office_id": oid, "type_id": tid, "last_inspect_date": d})
    admin_client.post("/equipment/new", data={"office_id": oid, "type_id": tid, "last_inspect_date": d})
    with app.app_context():
        nos = sorted(e.mgmt_no for e in Equipment.query.filter(Equipment.mgmt_no.like("SEL-GLV-%")).all())
        nums = [int(n.split("-")[-1]) for n in nos]
        assert nums == list(range(1, len(nums) + 1)) or len(set(nums)) == len(nums)
        assert nums[-1] == max(nums)


def test_inspection_updates_next_date(app, admin_client):
    with app.app_context():
        eq = Equipment.query.filter_by(last_result="합격").first()
        eid = eq.id
    today = date.today()
    r = admin_client.post(f"/equipment/{eid}/inspections", data={"inspect_date": today.isoformat(), "result": "합격",
                                                                  "inspector": "시험기관"})
    assert r.status_code == 302
    with app.app_context():
        eq = db.session.get(Equipment, eid)
        assert eq.last_inspect_date == today
        st = status_of(eq)
        assert st.next_inspect == add_months(today, eq.etype.inspect_months)
    admin_client.post(f"/equipment/{eid}/inspections", data={"inspect_date": today.isoformat(), "result": "불합격"})
    with app.app_context():
        assert status_of(db.session.get(Equipment, eid)).grade == "unusable"


def test_type_period_change_applies_immediately(app, admin_client):
    with app.app_context():
        t = EquipmentType.query.filter_by(code="GLV").first()
        eq = Equipment.query.filter_by(type_id=t.id).first()
        eid, tid = eq.id, t.id
        before = status_of(eq).next_inspect
        form = {"action": "save"}
        for x in EquipmentType.query.all():
            form[f"t{x.id}_name"] = x.name
            form[f"t{x.id}_inspect"] = 12 if x.id == tid else x.inspect_months
            form[f"t{x.id}_replace"] = ""
            form[f"t{x.id}_active"] = "1"
            form[f"t{x.id}_order"] = x.sort_order
    admin_client.post("/admin/types", data=form)
    with app.app_context():
        eq = db.session.get(Equipment, eid)
        assert status_of(eq).next_inspect == add_months(eq.last_inspect_date, 12) != before


def test_concurrent_edit_conflict(app, admin_client):
    with app.app_context():
        eq = Equipment.query.first()
        eid, ver = eq.id, eq.version
        base = {"office_id": eq.office_id, "type_id": eq.type_id, "last_inspect_date": eq.last_inspect_date.isoformat(),
                "last_result": eq.last_result, "status": eq.status, "version": ver}
    r1 = admin_client.post(f"/equipment/{eid}/edit", data={**base, "location": "A창고"})
    assert r1.status_code == 302
    r2 = admin_client.post(f"/equipment/{eid}/edit", data={**base, "location": "B창고"})  # 옛 version 으로 저장 시도
    assert r2.status_code == 409 and "먼저 수정했습니다" in r2.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Equipment, eid).location == "A창고"


def test_kpi_filter_links(admin_client):
    r = admin_client.get("/")
    body = r.get_data(as_text=True)
    assert "/equipment?grade=over" in body
    r = admin_client.get("/equipment?grade=over")
    body = r.get_data(as_text=True)
    assert "기한초과" in body
    assert "🟢 정상</span>" not in body.split("<tbody>")[1].split("</tbody>")[0]


def test_delete_requires_confirmation_and_audits(app, admin_client):
    with app.app_context():
        eq = Equipment.query.first()
        eid, no = eq.id, eq.mgmt_no
    admin_client.post(f"/equipment/{eid}/delete", data={"confirm_no": "WRONG"})
    with app.app_context():
        assert db.session.get(Equipment, eid) is not None
    admin_client.post(f"/equipment/{eid}/delete", data={"confirm_no": no})
    with app.app_context():
        assert db.session.get(Equipment, eid) is None
        assert AuditLog.query.filter_by(action="delete", target_type="equipment", target_id=eid).count() == 1


def _jpeg(w=3000, h=2000):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    buf.seek(0)
    return buf


def test_photo_resize_and_delete(app, admin_client):
    with app.app_context():
        eid = Equipment.query.first().id
    r = admin_client.post(f"/equipment/{eid}/photo", data={"photo": (_jpeg(), "big.jpg")},
                          content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        p = Photo.query.filter_by(equipment_id=eid).first()
        assert p and max(p.width, p.height) == 1280 and p.file_path.endswith(".jpg")
        pid = p.id
    assert admin_client.get(f"/photos/{pid}").status_code == 200
    body = admin_client.get(f"/equipment/{eid}").get_data(as_text=True)
    assert "실물 사진" in body
    admin_client.post(f"/equipment/{eid}/photo", data={"_method": "DELETE"})
    body = admin_client.get(f"/equipment/{eid}").get_data(as_text=True)
    assert "예시 그림" in body
    # 이미지가 아닌 파일 거부
    r = admin_client.post(f"/equipment/{eid}/photo", data={"photo": (io.BytesIO(b"not image"), "x.jpg")},
                          content_type="multipart/form-data", follow_redirects=True)
    assert "이미지 파일이 아니거나" in r.get_data(as_text=True)


def test_qr_scan_public_summary_and_redirect(app, client, admin_client):
    with app.app_context():
        eq = Equipment.query.filter(Equipment.manager_name.isnot(None)).first()
        no, mgr, eid = eq.mgmt_no, eq.manager_name, eq.id
        nxt = status_of(eq).next_inspect.isoformat()
    anon = app.test_client()
    r = anon.get(f"/q/{no}")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and no in body and nxt in body
    assert mgr not in body  # 개인정보 숨김
    assert anon.get(f"/qr/{no}.png").status_code == 302  # QR 이미지는 로그인 필요
    r = admin_client.get(f"/q/{no}")
    assert r.status_code == 302 and f"/equipment/{eid}" in r.headers["Location"]
    r = admin_client.get(f"/qr/{no}.png")
    assert r.status_code == 200 and r.mimetype == "image/png"


def test_label_print_24_per_sheet(app, admin_client):
    with app.app_context():
        ids = [e.id for e in Equipment.query.limit(30).all()]
    r = admin_client.get("/print/labels?" + "&".join(f"ids={i}" for i in ids))
    body = r.get_data(as_text=True)
    assert body.count('class="sheet"') == 2
    assert body.count('<div class="label">') == 30


def test_work_check_flags_and_saves(app, user_client):
    with app.app_context():
        bad = Equipment.query.filter_by(last_result="불합격").first().mgmt_no
        ok = None
        for e in Equipment.query.filter_by(last_result="합격", status="사용중").all():
            if status_of(e).effective_days > 40:
                ok = e.mgmt_no
                break
    r = user_client.post("/check", data={"work_date": date.today().isoformat(), "work_title": "보수",
                                          "nos": f"{ok}, {bad}, NOPE-XX-999"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "투입 불가 장비 있음" in body
    with app.app_context():
        wc = WorkCheck.query.order_by(WorkCheck.id.desc()).first()
        assert not wc.all_ok
        verdicts = {i.mgmt_no: i.verdict for i in wc.items}
        assert verdicts[ok] == "적합" and verdicts[bad] == "사용불가" and verdicts["NOPE-XX-999"] == "미등록"
        wid = wc.id
    assert user_client.get(f"/print/check/{wid}").status_code == 200
    assert "보수" in user_client.get("/check/history").get_data(as_text=True)


def test_pages_render(admin_client, user_client):
    for url in ("/", "/equipment", "/equipment?sort=next&dir=desc", "/equipment/1", "/equipment/1/edit",
                "/equipment/new", "/equipment/1/inspections", "/check", "/check/history", "/notify", "/notify/logs",
                "/labels", "/print/ledger", "/admin/users", "/admin/types", "/admin/offices", "/admin/settings",
                "/admin/import", "/admin/backup", "/admin/audit", "/roadmap", "/api/equipment"):
        r = admin_client.get(url)
        assert r.status_code == 200, url
    for url in ("/", "/equipment", "/check", "/roadmap", "/print/ledger"):
        assert user_client.get(url).status_code == 200, url
