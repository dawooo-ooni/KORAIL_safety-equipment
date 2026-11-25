"""작업 전 장비 점검 (판정 + 저장 + 이력 + 인쇄)."""
import re
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import Equipment, WorkCheck, WorkCheckItem
from ..services import audit
from ..services import settings as cfg
from ..services.calc import judge_for_work
from ..utils import clean, parse_date

bp = Blueprint("check", __name__)

VERDICT_CSS = {"적합": "ok", "만료": "over", "사용불가": "dead", "미등록": "dead"}
VERDICT_LABEL = {"적합": "✅ 적합", "만료": "❌ 기한만료", "사용불가": "⛔ 사용불가", "미등록": "❓ 미등록"}


def _equipment_list():
    return (Equipment.query.options(joinedload(Equipment.office), joinedload(Equipment.etype))
            .order_by(Equipment.mgmt_no).all())


@bp.route("/check", methods=["GET", "POST"])
@login_required
def index():
    result = None
    form = {"work_date": date.today().isoformat(), "work_title": "", "nos": ""}
    errors = []
    if request.method == "POST":
        f = request.form
        wd = parse_date(f.get("work_date"))
        title = clean(f.get("work_title"), 200)
        raw = (f.get("nos") or "") + " " + " ".join(f.getlist("pick"))
        nos = list(dict.fromkeys(s.strip().upper() for s in re.split(r"[\s,;]+", raw) if s.strip()))
        form = {"work_date": f.get("work_date") or "", "work_title": title, "nos": ", ".join(nos)}
        if not wd:
            errors.append("작업일자를 입력하세요.")
        if not nos:
            errors.append("투입할 장비를 입력하거나 선택하세요.")
        if len(nos) > 300:
            errors.append("한 번에 300개까지 점검할 수 있습니다.")
        if not errors:
            u, w = cfg.thresholds()
            wc = WorkCheck(work_date=wd, work_title=title or None, checked_by=current_user.id, all_ok=True)
            db.session.add(wc)
            for no in nos:
                eq = Equipment.query.filter(db.func.upper(Equipment.mgmt_no) == no).first()
                if not eq:
                    item = WorkCheckItem(mgmt_no=no[:30], verdict="미등록", reason="대장에 없는 관리번호")
                else:
                    verdict, reason, st = judge_for_work(eq, eq.etype, wd, u, w)
                    item = WorkCheckItem(mgmt_no=eq.mgmt_no, equipment_id=eq.id, verdict=verdict,
                                         reason=reason[:200] or None, next_inspect_date=st.next_inspect)
                if item.verdict != "적합":
                    wc.all_ok = False
                wc.items.append(item)
            db.session.flush()
            audit.log("work_check", "work_check", wc.id, None,
                      {"work_date": wd.isoformat(), "title": title, "count": len(nos), "all_ok": wc.all_ok})
            db.session.commit()
            if not wc.all_ok:
                flash("⚠ 투입 불가 장비가 있습니다. 결과를 확인하세요.", "error")
            else:
                flash("점검 결과를 저장했습니다.", "ok")
            return redirect(url_for("check.index", id=wc.id) + "#result")
    if request.args.get("id", type=int):
        result = db.session.get(WorkCheck, request.args.get("id", type=int))
    return render_template("check/index.html", form=form, errors=errors, equipment=_equipment_list(),
                           result=result, css=VERDICT_CSS, label=VERDICT_LABEL)


@bp.route("/check/history")
@login_required
def history():
    page = max(request.args.get("page", 1, type=int), 1)
    q = WorkCheck.query.order_by(WorkCheck.work_date.desc(), WorkCheck.id.desc())
    if request.args.get("bad") == "1":
        q = q.filter(WorkCheck.all_ok.is_(False))
    pg = q.paginate(page=page, per_page=30, error_out=False)
    return render_template("check/history.html", pg=pg)


@bp.route("/print/check/<int:cid>")
@login_required
def print_check(cid):
    wc = db.session.get(WorkCheck, cid) or abort(404)
    return render_template("print/check.html", wc=wc, css=VERDICT_CSS, label=VERDICT_LABEL)
