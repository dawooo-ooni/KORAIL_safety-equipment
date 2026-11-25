"""11장 수용 기준 — 계산"""
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from app.services.calc import add_months, dd_text, evaluate, judge_for_work

REF = date(2026, 11, 25)
T6 = NS(inspect_months=6, replace_months=None)


def eq_with_days_left(days, result="합격", status="사용중", issue=None):
    nxt = REF + timedelta(days=days)
    return NS(last_inspect_date=add_months(nxt, -6), issue_date=issue, last_result=result, status=status), nxt


def test_add_months_month_end():
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)
    assert add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


@pytest.mark.parametrize("days,grade", [(-1, "over"), (0, "urgent"), (7, "urgent"), (8, "warn"), (30, "warn"), (31, "ok")])
def test_grade_boundaries(days, grade):
    # 말일 보정 때문에 역산이 정확하지 않을 수 있어, 직접 last_inspect_date 를 찾아 맞춘다
    target = REF + timedelta(days=days)
    last = None
    d = target - timedelta(days=200)
    while d <= target:
        if add_months(d, 6) == target:
            last = d
            break
        d += timedelta(days=1)
    assert last is not None
    st = evaluate(NS(last_inspect_date=last, issue_date=None, last_result="합격", status="사용중"), T6, REF, 7, 30)
    assert st.days_left == days
    assert st.grade == grade


def test_unusable_regardless_of_date():
    e, _ = eq_with_days_left(100, result="불합격")
    assert evaluate(e, T6, REF).grade == "unusable"
    e, _ = eq_with_days_left(-50, status="폐기")
    st = evaluate(e, T6, REF)
    assert st.grade == "unusable" and st.reason == "폐기"


def test_replace_due_passed_is_over():
    t = NS(inspect_months=6, replace_months=12)
    e = NS(last_inspect_date=REF - timedelta(days=10), issue_date=add_months(REF, -12) - timedelta(days=1),
           last_result="합격", status="사용중")
    st = evaluate(e, t, REF)
    assert st.grade == "over"
    assert st.reason == "교체시기 경과"
    assert st.effective_days == st.replace_days_left < 0


def test_dd_text():
    assert dd_text(12) == "D-12"
    assert dd_text(0) == "D-Day"
    assert dd_text(-5) == "D+5 초과"


def test_work_judgement():
    e, nxt = eq_with_days_left(3)
    assert judge_for_work(e, T6, REF)[0] == "적합"
    assert "임박" in judge_for_work(e, T6, REF)[1]
    assert judge_for_work(e, T6, nxt + timedelta(days=1))[0] == "만료"
    e2, _ = eq_with_days_left(100, status="수리중")
    assert judge_for_work(e2, T6, REF)[0] == "사용불가"
