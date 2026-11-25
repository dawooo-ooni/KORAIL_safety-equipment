"""담당자 알림: 미리보기, 수동 발송, 발송 이력, 재발송."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import NotificationLog
from ..services import alerts
from ..services import settings as cfg
from ..utils import admin_required

bp = Blueprint("notify", __name__)


@bp.route("/notify")
@admin_required
def index():
    groups = alerts.build_groups()
    state, state_msg = alerts.mail_state()
    previews = [(g, alerts.subject_for(g), alerts.body_for(g),
                 alerts.already_sent_today(g.recipient) if g.recipient else False) for g in groups]
    return render_template("notify/index.html", previews=previews, state=state, state_msg=state_msg,
                           total_items=sum(len(g.rows) for g in groups),
                           alert_enabled=cfg.get_bool("alert_enabled"), alert_time=cfg.get("alert_time"),
                           alert_cc=cfg.get("alert_cc"))


@bp.route("/notify/send", methods=["POST"])
@admin_required
def send():
    state, msg = alerts.mail_state()
    if state == "unset":
        flash(msg, "error")
        return redirect(url_for("notify.index"))
    recipients = request.form.getlist("recipient")
    if not recipients:
        flash("발송할 담당자를 선택하세요.", "error")
        return redirect(url_for("notify.index"))
    force = request.form.get("force") == "1"
    res = alerts.run(trigger="manual", only=recipients, sent_by=current_user.id, force=force)
    ok = sum(1 for r in res if r["status"] == "sent")
    skipped = [r for r in res if r["status"] == "skipped"]
    failed = [r for r in res if r["status"] == "failed"]
    flash(f"발송 {ok}건" + (f", 건너뜀 {len(skipped)}건" if skipped else "") + (f", 실패 {len(failed)}건" if failed else "")
          + (" (콘솔 출력 모드: 서버 콘솔을 확인하세요)" if state == "console" else ""), "error" if failed else "ok")
    for r in skipped:
        flash(f"건너뜀: {r['recipient']} — {r.get('error')}", "info")
    return redirect(url_for("notify.logs"))


@bp.route("/notify/logs")
@admin_required
def logs():
    page = max(request.args.get("page", 1, type=int), 1)
    q = NotificationLog.query.order_by(NotificationLog.sent_at.desc(), NotificationLog.id.desc())
    if request.args.get("status"):
        q = q.filter(NotificationLog.status == request.args["status"])
    pg = q.paginate(page=page, per_page=50, error_out=False)
    return render_template("notify/logs.html", pg=pg, status=request.args.get("status", ""))


@bp.route("/notify/logs/<int:lid>/resend", methods=["POST"])
@admin_required
def resend(lid):
    log = db.get_or_404(NotificationLog, lid)
    state, msg = alerts.mail_state()
    if state == "unset":
        flash(msg, "error")
        return redirect(url_for("notify.logs"))
    res = alerts.run(trigger="manual", only=[log.recipient], sent_by=current_user.id, force=True)
    if not res:
        flash("현재 이 수신자에게 보낼 알림 대상 장비가 없습니다.", "info")
    else:
        r = res[0]
        flash(f"재발송: {r['recipient']} → {r['status']}" + (f" ({r.get('error')})" if r.get("error") else ""),
              "ok" if r["status"] == "sent" else "error")
    return redirect(url_for("notify.logs"))
