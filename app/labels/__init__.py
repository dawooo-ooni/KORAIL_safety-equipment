"""QR 라벨 선택·인쇄, QR 이미지, QR 스캔(공개) 페이지."""
from datetime import date

from flask import Blueprint, Response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import Equipment
from ..services.ledger import all_rows, lookups, sort_rows, status_of
from ..services.qr import qr_png, qr_url
from ..utils import admin_required

bp = Blueprint("labels", __name__)

PER_SHEET = 24  # A4 3열 × 8행


@bp.route("/labels")
@admin_required
def index():
    rows = all_rows()
    office = request.args.get("office", "")
    grade = request.args.get("grade", "")
    rows = [r for r in rows if (not office or r.eq.office.code == office) and (not grade or r.st.grade == grade)]
    rows = sort_rows(rows, "mgmt_no")
    offices, _ = lookups()
    return render_template("labels/index.html", rows=rows, offices=offices, office=office, grade=grade)


@bp.route("/print/labels")
@admin_required
def print_labels():
    ids = [int(x) for x in request.args.getlist("ids") if str(x).isdigit()]
    ids += [int(x) for x in (request.args.get("idlist") or "").split(",") if x.strip().isdigit()]
    eqs = Equipment.query.filter(Equipment.id.in_(ids)).order_by(Equipment.mgmt_no).all() if ids else []
    items = [(e, status_of(e)) for e in eqs]
    skip = min(max(request.args.get("skip", 0, type=int), 0), PER_SHEET - 1)  # 이미 쓴 칸 건너뛰기
    sheets = []
    cells = [None] * skip + items
    for i in range(0, len(cells), PER_SHEET):
        sheets.append(cells[i:i + PER_SHEET])
    return render_template("print/labels.html", sheets=sheets, count=len(items), issued=date.today(), skip=skip)


@bp.route("/qr/<path:mgmt_no>.png")
@login_required
def qr_image(mgmt_no):
    eq = Equipment.query.filter_by(mgmt_no=mgmt_no.upper()).first_or_404()
    png = qr_png(qr_url(eq.mgmt_no), box_size=int(request.args.get("s", 8) or 8))
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=86400"})


@bp.route("/q/<path:mgmt_no>")
def scan(mgmt_no):
    """QR 스캔 페이지. 비로그인은 요약 카드만, 로그인 상태면 상세로 이동."""
    eq = Equipment.query.filter_by(mgmt_no=mgmt_no.strip().upper()).first()
    if eq is None:
        return render_template("labels/scan.html", eq=None, mgmt_no=mgmt_no), 404
    if current_user.is_authenticated:
        return redirect(url_for("equipment.detail", eid=eq.id, scan=1))
    return render_template("labels/scan.html", eq=eq, st=status_of(eq), mgmt_no=eq.mgmt_no)
