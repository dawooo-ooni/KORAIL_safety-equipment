"""(선택) 읽기 전용 JSON API — 향후 모바일 앱·사내 시스템 연동용. 로그인 세션 필요."""
from flask import Blueprint, jsonify, request
from flask_login import login_required

from ..models import Equipment
from ..services.calc import GRADES
from ..services.ledger import all_rows, filter_rows, sort_rows, status_of

bp = Blueprint("api", __name__, url_prefix="/api")


def _row(eq, st):
    return {
        "id": eq.id, "mgmt_no": eq.mgmt_no, "office": eq.office.code, "office_name": eq.office.name,
        "type": eq.etype.code, "type_name": eq.etype.name, "status": eq.status, "last_result": eq.last_result,
        "last_inspect_date": eq.last_inspect_date.isoformat(),
        "next_inspect_date": st.next_inspect.isoformat(),
        "replace_due": st.replace_due.isoformat() if st.replace_due else None,
        "days_left": st.days_left, "effective_days": st.effective_days,
        "grade": st.grade, "grade_label": GRADES[st.grade]["label"], "reason": st.reason,
    }


@bp.route("/equipment")
@login_required
def equipment_list():
    rows = sort_rows(filter_rows(all_rows(), request.args), request.args.get("sort", "eff"))
    return jsonify(count=len(rows), items=[_row(r.eq, r.st) for r in rows])


@bp.route("/equipment/<mgmt_no>")
@login_required
def equipment_one(mgmt_no):
    eq = Equipment.query.filter_by(mgmt_no=mgmt_no.upper()).first_or_404()
    d = _row(eq, status_of(eq))
    d["inspections"] = [{"date": i.inspect_date.isoformat(), "result": i.result, "inspector": i.inspector,
                         "memo": i.memo} for i in eq.inspections]
    return jsonify(d)
