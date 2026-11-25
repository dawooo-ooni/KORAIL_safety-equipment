"""차기검사일·남은 일수·경보 등급 계산 (작업지시서 6장, 시제품과 같은 규칙).

모두 순수 함수다. DB에 저장하지 않고 조회할 때마다 계산하므로
기준표(검사주기)를 바꾸면 전체 장비에 즉시 반영된다.
"""
import calendar
from dataclasses import dataclass
from datetime import date


def add_months(d: date, months: int) -> date:
    """월 단위 더하기. 말일 보정 (예: 8/31 + 6개월 = 2/28 또는 2/29)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass
class Status:
    next_inspect: date | None
    replace_due: date | None
    days_left: int | None          # 차기검사일 - 기준일
    replace_days_left: int | None
    effective_days: int | None     # 둘 중 작은 값
    grade: str                     # over / urgent / warn / ok / unusable
    reason: str

    @property
    def g(self):
        return GRADES[self.grade]


# 등급 표시 정보. css 는 시제품 CSS 클래스 접미사(over/urg/warn/ok/dead)
GRADES = {
    "over": {"key": "over", "label": "기한초과", "icon": "🔴", "css": "over", "color": "#c62828"},
    "urgent": {"key": "urgent", "label": "긴급", "icon": "🟠", "css": "urg", "color": "#d35400"},
    "warn": {"key": "warn", "label": "주의", "icon": "🟡", "css": "warn", "color": "#e9b800"},
    "ok": {"key": "ok", "label": "정상", "icon": "🟢", "css": "ok", "color": "#2e7d32"},
    "unusable": {"key": "unusable", "label": "사용불가", "icon": "⚫", "css": "dead", "color": "#37474f"},
}
GRADE_ORDER = {"over": 0, "urgent": 1, "warn": 2, "unusable": 3, "ok": 4}
ACTION_GRADES = ("over", "urgent", "warn")  # 알림 대상


def evaluate(eq, etype, ref: date, urgent_days: int = 7, warn_days: int = 30) -> Status:
    """eq: last_inspect_date, issue_date, last_result, status 속성을 가진 객체.
    etype: inspect_months, replace_months 속성을 가진 객체."""
    nxt = add_months(eq.last_inspect_date, etype.inspect_months)
    rep = add_months(eq.issue_date, etype.replace_months) if (eq.issue_date and etype.replace_months) else None
    dl = (nxt - ref).days
    rdl = (rep - ref).days if rep else None
    eff = min(x for x in (dl, rdl) if x is not None)

    if eq.last_result == "불합격":
        return Status(nxt, rep, dl, rdl, eff, "unusable", "검사 불합격")
    if eq.status == "폐기":
        return Status(nxt, rep, dl, rdl, eff, "unusable", "폐기")
    if eff < 0:
        reason = "교체시기 경과" if (rdl is not None and rdl < 0) else "검사기한 경과"
        return Status(nxt, rep, dl, rdl, eff, "over", reason)
    if eff <= urgent_days:
        return Status(nxt, rep, dl, rdl, eff, "urgent", "교체 임박" if rdl == eff else "검사 임박")
    if eff <= warn_days:
        return Status(nxt, rep, dl, rdl, eff, "warn", "교체 예정" if rdl == eff else "검사 예정")
    return Status(nxt, rep, dl, rdl, eff, "ok", "")


def dd_text(days) -> str:
    """D-day 표기: D-12, D-Day, D+5 초과"""
    if days is None:
        return "-"
    if days == 0:
        return "D-Day"
    return f"D-{days}" if days > 0 else f"D+{-days} 초과"


def judge_for_work(eq, etype, work_date: date, urgent_days: int = 7, warn_days: int = 30):
    """작업 전 점검 판정. 반환: (verdict, reason, Status)
    사용불가: 불합격 또는 상태 폐기·수리중 / 만료: 작업일 기준 effective_days < 0 / 그 밖은 적합"""
    st = evaluate(eq, etype, work_date, urgent_days, warn_days)
    if eq.last_result == "불합격":
        return "사용불가", "최종검사 불합격", st
    if eq.status in ("폐기", "수리중"):
        return "사용불가", f"상태: {eq.status}", st
    if st.effective_days < 0:
        return "만료", st.reason or "작업일 기준 검사기한 경과", st
    if st.effective_days <= urgent_days:
        what = "교체" if st.replace_days_left == st.effective_days else "검사"
        return "적합", f"작업일 기준 {what} 임박 ({dd_text(st.effective_days)})", st
    return "적합", "", st
