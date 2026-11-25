"""11장 수용 기준 — 데이터 입출력, 알림, 백업"""
import io
import json

from openpyxl import load_workbook

from app.extensions import db
from app.models import Equipment, Inspection, NotificationLog
from app.services import alerts
from app.services.backup import backup_db, export_json, import_legacy, restore_json

CSV_HEAD = "관리번호,사업소,장비구분,모델/규격,제조번호,지급일,최종검사일,최종검사결과,상태,담당자,담당자이메일,보관위치,비고"


def _upload(client, data: bytes, name: str):
    r = client.post("/admin/import", data={"action": "upload", "file": (io.BytesIO(data), name)},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)[:500]
    body = r.get_data(as_text=True)
    token = body.split('name="token" value="')[1].split('"')[0]
    return body, token


def test_csv_import_utf8_bom_and_cp949(app, admin_client):
    rows = [
        "# 작성 안내 줄",
        CSV_HEAD,
        ",서울고속전기사업소(샘플),절연장갑,모델A,SN1,2025-03-02,2026.05.15,합격,사용중,홍길동,hong@example.com,보호구함,",
        ",DJN,DHV,,,,20260601,합격,보관,,,,",
        ",없는사업소,GLV,,,,2026-05-01,합격,사용중,,,,",
    ]
    text = "\r\n".join(rows)
    for raw, name in ((("﻿" + text).encode("utf-8"), "a.csv"), (text.encode("cp949"), "b.csv")):
        with app.app_context():
            n0 = Equipment.query.count()
        body, token = _upload(admin_client, raw, name)
        assert "신규 2건" in body and "오류 1건" in body
        r = admin_client.post("/admin/import", data={"action": "confirm", "token": token})
        assert r.status_code == 302
        with app.app_context():
            assert Equipment.query.count() == n0 + 2
            e = Equipment.query.filter_by(serial_no="SN1").order_by(Equipment.id.desc()).first()
            assert e.mgmt_no.startswith("SEL-GLV-") and e.last_inspect_date.isoformat() == "2026-05-15"


def test_xlsx_roundtrip_keeps_data(app, admin_client):
    r = admin_client.get("/admin/export.xlsx")
    assert r.status_code == 200
    with app.app_context():
        before = {e.mgmt_no: (e.office_id, e.type_id, e.last_inspect_date, e.last_result, e.status, e.manager_name,
                              e.manager_email, e.location, e.version) for e in Equipment.query.all()}
        n_insp = Inspection.query.count()
    body, token = _upload(admin_client, r.data, "export.xlsx")
    assert f"갱신 {len(before)}건" in body and "오류 0건" in body
    admin_client.post("/admin/import", data={"action": "confirm", "token": token})
    with app.app_context():
        after = {e.mgmt_no: (e.office_id, e.type_id, e.last_inspect_date, e.last_result, e.status, e.manager_name,
                             e.manager_email, e.location) for e in Equipment.query.all()}
        assert {k: v[:8] for k, v in before.items()} == after
        assert Inspection.query.count() == n_insp  # 최종검사일 그대로면 이력 추가 없음


def test_export_csv_has_bom(admin_client):
    r = admin_client.get("/admin/export.csv")
    assert r.data.startswith("﻿".encode("utf-8"))
    assert "관리번호" in r.data.decode("utf-8-sig").splitlines()[0]


def test_template_xlsx(admin_client):
    r = admin_client.get("/admin/import/template.xlsx")
    wb = load_workbook(io.BytesIO(r.data))
    assert wb.active["A1"].value == "관리번호"


def test_import_legacy_json(app):
    legacy = {
        "settings": {"offices": [{"code": "GJU", "name": "광주사업소"}],
                     "types": [{"code": "GLV", "name": "절연장갑", "inspectMonths": 6, "replaceMonths": ""},
                               {"code": "NEW", "name": "신규구분", "inspectMonths": 3, "replaceMonths": 24}],
                     "urgentDays": 5, "warnDays": 20},
        "items": [{"id": "i1", "no": "GJU-NEW-001", "office": "GJU", "type": "NEW", "model": "M", "serial": "S",
                   "issueDate": "2025-01-01", "lastInspect": "2026-10-01", "result": "합격", "status": "사용중",
                   "manager": "홍", "email": "h@example.com", "location": "L", "note": "",
                   "history": [{"date": "2026-04-01", "result": "합격", "inspector": "기관", "memo": ""},
                               {"date": "2026-10-01", "result": "합격", "inspector": "기관", "memo": ""}]}],
        "users": [{"login": "x"}],
    }
    with app.app_context():
        r = import_legacy(legacy)
        assert r["new"] == 1 and not r["errors"]
        e = Equipment.query.filter_by(mgmt_no="GJU-NEW-001").first()
        assert e and len(e.inspections) == 2 and e.etype.inspect_months == 3
        from app.services import settings as cfg
        assert cfg.get("urgent_days") == "5"


def test_json_backup_restore(app):
    with app.app_context():
        data = export_json()
        n = Equipment.query.count()
        db.session.delete(Equipment.query.first())
        db.session.commit()
        data2 = json.loads(json.dumps(data))
        new, upd, deleted = restore_json(data2)
        assert Equipment.query.count() == n and new == 1 and deleted == 0


def test_db_backup_file(app):
    with app.app_context():
        p = backup_db()
        import os
        assert os.path.getsize(p) > 0


def test_alerts_dry_run_and_no_duplicate(app, capsys):
    with app.app_context():
        out = io.StringIO()
        res = alerts.run(dry_run=True, out=out)
        assert res and all(r["status"] == "dry-run" for r in res)
        text = out.getvalue()
        assert "[안전장비 검사기한 알림]" in text and "관리번호 | 장비구분" in text
        assert NotificationLog.query.count() == 0
        r1 = alerts.run(trigger="auto")
        sent1 = [r for r in r1 if r["status"] == "sent"]
        assert sent1
        r2 = alerts.run(trigger="auto")
        assert not [r for r in r2 if r["status"] == "sent"]
        assert all(r["status"] == "skipped" for r in r2 if r["recipient"] != "(없음)")


def test_alert_groups_by_email(app):
    with app.app_context():
        groups = alerts.build_groups()
        emails = [g.recipient.lower() for g in groups if g.recipient]
        assert len(emails) == len(set(emails))
        subj = alerts.subject_for(groups[0])
        assert subj.startswith("[안전장비 검사기한 알림]") and "조치 요청" in subj


def test_cron_endpoint_requires_secret(app):
    from app.services import settings as cfg
    c = app.test_client()
    assert c.get("/cron/daily").status_code == 401
    assert c.get("/cron/daily", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.get("/cron/daily", headers={"Authorization": "Bearer test-cron"})
    assert r.status_code == 200 and "꺼짐" in r.get_json()["skipped"]
    with app.app_context():
        cfg.set("alert_enabled", "1")
        db.session.commit()
    r = c.get("/cron/daily", headers={"Authorization": "Bearer test-cron"})
    assert r.status_code == 200 and r.get_json()["results"]


def test_db_url_normalized():
    from app.config import _db_url, _engine_options
    import os
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "postgresql://u:p@h:6543/postgres"
    try:
        assert _db_url() == "postgresql+psycopg://u:p@h:6543/postgres"
        assert _engine_options(_db_url())["connect_args"]["prepare_threshold"] is None
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL")
        else:
            os.environ["DATABASE_URL"] = old


def test_send_alerts_cli(app):
    runner = app.test_cli_runner()
    r = runner.invoke(args=["send-alerts", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "DRY-RUN" in r.output
