"""알림 대상 추출, 메일 본문 생성, 발송 (작업지시서 8.2).

발송 채널은 send(recipient, subject, body, html=None) 인터페이스를 따른다.
이번 범위는 메일(콘솔/SMTP)만 구현. 사내 메신저는 같은 인터페이스로 추가하면 된다.
"""
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from email.message import EmailMessage
from html import escape

from flask import current_app

from ..extensions import db
from ..models import NotificationLog, User
from . import settings as cfg
from .calc import ACTION_GRADES, dd_text
from .ledger import all_rows, sort_rows


# ---------------- 발송 채널 ----------------

class Channel:
    name = "base"

    def send(self, recipient, subject, body, html=None, cc=None):  # pragma: no cover - 인터페이스
        raise NotImplementedError


class ConsoleMailChannel(Channel):
    """MAIL_BACKEND=console: 실제 발송하지 않고 콘솔에 출력"""
    name = "console"

    def send(self, recipient, subject, body, html=None, cc=None):
        out = sys.stdout
        print("=" * 70, file=out)
        print(f"[메일-콘솔] To: {recipient}" + (f"  Cc: {', '.join(cc)}" if cc else ""), file=out)
        print(f"Subject: {subject}", file=out)
        print("-" * 70, file=out)
        print(body, file=out)
        print("=" * 70, file=out, flush=True)


class SmtpMailChannel(Channel):
    name = "smtp"

    def __init__(self, conf):
        self.c = conf

    def send(self, recipient, subject, body, html=None, cc=None):
        msg = EmailMessage()
        msg["From"] = self.c["MAIL_FROM"]
        msg["To"] = recipient
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.c["SMTP_HOST"], self.c["SMTP_PORT"], timeout=20) as s:
            if self.c["SMTP_USE_TLS"]:
                s.starttls()
            if self.c["SMTP_USER"]:
                s.login(self.c["SMTP_USER"], self.c["SMTP_PASSWORD"])
            s.send_message(msg)


def mail_state():
    """반환: ('console'|'smtp'|'unset', 설명)"""
    c = current_app.config
    if c["MAIL_BACKEND"] == "console":
        return "console", "콘솔 출력 모드 (MAIL_BACKEND=console) — 실제 메일은 발송되지 않고 서버 콘솔에 출력됩니다."
    if c["MAIL_BACKEND"] == "smtp" and c["SMTP_HOST"]:
        return "smtp", f"SMTP 발송 ({c['SMTP_HOST']}:{c['SMTP_PORT']})"
    return "unset", "SMTP 미설정 — .env 의 MAIL_BACKEND=smtp 와 SMTP_HOST 등을 설정해야 메일이 발송됩니다."


def get_channel():
    state, _ = mail_state()
    if state == "console":
        return ConsoleMailChannel()
    if state == "smtp":
        return SmtpMailChannel(current_app.config)
    return None


def send_simple(recipient, subject, body):
    """가입 승인·반려 등 단건 안내 메일. 실패해도 예외를 올리지 않는다."""
    ch = get_channel()
    if not ch or not recipient:
        return False
    try:
        ch.send(recipient, subject, body)
        return True
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("메일 발송 실패 %s: %s", recipient, e)
        return False


# ---------------- 대상 묶기 ----------------

@dataclass
class Group:
    recipient: str            # 이메일 ('' 이면 수신자 없음)
    name: str                 # 받는 사람 이름
    via_office_admin: bool = False
    rows: list = field(default_factory=list)

    @property
    def over_count(self):
        return sum(1 for r in self.rows if r.st.grade == "over")


def build_groups(ref=None):
    """기한초과·긴급·주의 장비를 담당자 이메일별로 묶는다.
    담당자 이메일이 없으면 해당 사업소 관리자(이메일 있는 활성 관리자)에게 보낸다."""
    ref = ref or date.today()
    rows = [r for r in all_rows(ref) if r.st.grade in ACTION_GRADES]
    rows = sort_rows(rows, "eff")
    admins_by_office = {}
    for u in User.query.filter_by(role="admin", status="active").all():
        if u.email:
            admins_by_office.setdefault(u.office_id, []).append(u)
    groups = {}
    for r in rows:
        email = r.eq.manager_mail
        if email:
            key = email.lower()
            g = groups.get(key) or groups.setdefault(key, Group(email, r.eq.manager_display or email))
            g.rows.append(r)
            continue
        admins = admins_by_office.get(r.eq.office_id) or []
        if not admins:
            key = f"__none__{r.eq.office_id}"
            g = groups.setdefault(key, Group("", f"{r.eq.office.name} (담당자 이메일 없음)", True))
            g.rows.append(r)
            continue
        for a in admins:
            key = a.email.lower()
            g = groups.get(key) or groups.setdefault(key, Group(a.email, a.name, True))
            g.rows.append(r)
    return list(groups.values())


def subject_for(g, ref=None):
    ref = ref or date.today()
    return f"[안전장비 검사기한 알림] {g.name}님 담당 장비 {len(g.rows)}건 조치 요청 ({ref.isoformat()})"


def body_for(g, ref=None):
    ref = ref or date.today()
    _, warn = cfg.thresholds()
    base = current_app.config["APP_BASE_URL"]
    lines = [
        f"{r.eq.mgmt_no} | {r.eq.etype.name} | {r.eq.office.name} | {r.st.next_inspect.isoformat()} | "
        f"{dd_text(r.st.effective_days)}{' (' + r.st.reason + ')' if r.st.reason else ''}"
        for r in g.rows
    ]
    extra = "\n(담당자 이메일이 등록되지 않은 장비라 사업소 관리자님께 보내 드립니다.)\n" if g.via_office_admin else ""
    return (
        f"{g.name}님, 안녕하세요.\n{extra}\n"
        f"담당하시는 안전장비·보호구 중 검사(교체) 기한이 지났거나 {warn}일 이내에 도래하는 장비가 있어 알려 드립니다. "
        f"(기준일: {ref.isoformat()})\n\n"
        "관리번호 | 장비구분 | 사업소 | 차기검사일 | D-day\n" + "-" * 48 + "\n" + "\n".join(lines) + "\n\n"
        "위 장비의 검사 일정을 확인해 주시고, 기한이 지난 장비는 즉시 사용을 중지한 뒤 검사 또는 교체 조치를 해 주시기 바랍니다.\n"
        "조치가 끝나면 관리 대장에 검사 결과를 등록해 주세요.\n\n"
        f"관리 대장 바로가기: {base}/equipment?grade=over\n\n감사합니다."
    )


def html_for(g, ref=None):
    ref = ref or date.today()
    _, warn = cfg.thresholds()
    base = current_app.config["APP_BASE_URL"]
    colors = {"over": "#c62828", "urgent": "#d35400", "warn": "#8a6100"}
    trs = "".join(
        f"<tr><td style='padding:4px 8px;border:1px solid #ccd'><b>{escape(r.eq.mgmt_no)}</b></td>"
        f"<td style='padding:4px 8px;border:1px solid #ccd'>{escape(r.eq.etype.name)}</td>"
        f"<td style='padding:4px 8px;border:1px solid #ccd'>{escape(r.eq.office.name)}</td>"
        f"<td style='padding:4px 8px;border:1px solid #ccd'>{r.st.next_inspect.isoformat()}</td>"
        f"<td style='padding:4px 8px;border:1px solid #ccd;color:{colors.get(r.st.grade, '#000')};font-weight:700'>"
        f"{escape(dd_text(r.st.effective_days))}</td></tr>"
        for r in g.rows
    )
    return (
        f"<div style='font-family:Malgun Gothic,sans-serif;font-size:14px'>"
        f"<p>{escape(g.name)}님, 안녕하세요.</p>"
        f"<p>담당하시는 안전장비·보호구 중 검사(교체) 기한이 지났거나 {warn}일 이내에 도래하는 장비가 있어 알려 드립니다. "
        f"(기준일: {ref.isoformat()})</p>"
        "<table style='border-collapse:collapse;font-size:13px'><thead><tr style='background:#e8eef8'>"
        "<th style='padding:4px 8px;border:1px solid #ccd'>관리번호</th><th style='padding:4px 8px;border:1px solid #ccd'>장비구분</th>"
        "<th style='padding:4px 8px;border:1px solid #ccd'>사업소</th><th style='padding:4px 8px;border:1px solid #ccd'>차기검사일</th>"
        f"<th style='padding:4px 8px;border:1px solid #ccd'>D-day</th></tr></thead><tbody>{trs}</tbody></table>"
        "<p>위 장비의 검사 일정을 확인해 주시고, 기한이 지난 장비는 즉시 사용을 중지한 뒤 검사 또는 교체 조치를 해 주시기 바랍니다.<br>"
        "조치가 끝나면 관리 대장에 검사 결과를 등록해 주세요.</p>"
        f"<p><a href='{escape(base)}/equipment?grade=over'>관리 대장 바로가기</a></p><p>감사합니다.</p></div>"
    )


def already_sent_today(recipient, today=None):
    today = today or date.today()
    start = datetime.combine(today, time.min)
    return db.session.query(NotificationLog.id).filter(
        NotificationLog.recipient == recipient,
        NotificationLog.status == "sent",
        NotificationLog.item_count > 0,
        NotificationLog.sent_at >= start,
    ).first() is not None


def run(trigger="auto", only=None, dry_run=False, sent_by=None, force=False, out=None):
    """알림 실행. only: 보낼 수신자 이메일 목록(None 이면 전체).
    같은 날 같은 수신자에게 이미 'sent' 기록이 있으면 건너뛴다 (force=True 는 수동 재발송).
    반환: 결과 dict 목록"""
    ref = date.today()
    groups = build_groups(ref)
    if only is not None:
        wanted = {e.lower() for e in only}
        groups = [g for g in groups if g.recipient and g.recipient.lower() in wanted]
    ch = None if dry_run else get_channel()
    cc = [x.strip() for x in cfg.get("alert_cc").replace(";", ",").split(",") if x.strip()]
    results = []
    for g in groups:
        subj, body = subject_for(g, ref), body_for(g, ref)
        res = {"recipient": g.recipient or "(없음)", "name": g.name, "count": len(g.rows), "subject": subj}
        if dry_run:
            res["status"] = "dry-run"
            if out is not None:
                print("=" * 70, file=out)
                print(f"[DRY-RUN] To: {g.recipient or '(수신자 없음)'}  ({len(g.rows)}건)", file=out)
                if cc:
                    print(f"Cc: {', '.join(cc)}", file=out)
                print(f"Subject: {subj}", file=out)
                print("-" * 70, file=out)
                print(body, file=out)
            results.append(res)
            continue
        status, error = "sent", None
        if not g.recipient:
            status, error = "skipped", "수신자 이메일 없음 (담당자·사업소 관리자 이메일 미등록)"
        elif ch is None:
            status, error = "skipped", "SMTP 미설정"
        elif not force and already_sent_today(g.recipient, ref):
            status, error = "skipped", "오늘 이미 발송함 (중복 방지)"
        else:
            try:
                ch.send(g.recipient, subj, body, html=html_for(g, ref), cc=cc or None)
                if ch.name == "console":
                    error = "콘솔 출력 모드"
            except Exception as e:  # noqa: BLE001 - 실패는 기록하고 다음 수신자로
                status, error = "failed", str(e)[:500]
        db.session.add(NotificationLog(
            trigger=trigger, recipient=(g.recipient or "(없음)")[:120], subject=subj[:200],
            item_count=len(g.rows), status=status, error=error, sent_by=sent_by,
        ))
        db.session.commit()
        res.update(status=status, error=error)
        results.append(res)
    return results
