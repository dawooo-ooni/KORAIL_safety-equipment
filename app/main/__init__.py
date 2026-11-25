"""대시보드, 개발 목표, 로고 파일 제공."""
import io
import os

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from flask_login import login_required

from ..models import Office
from ..services import settings as cfg
from ..services.storage import get_store
from ..services.ledger import all_rows, grade_counts, sort_rows

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def dashboard():
    rows = all_rows()
    cnt = grade_counts(rows)
    offices = Office.query.order_by(Office.code).all()
    per_office = []
    for o in offices:
        c = grade_counts([r for r in rows if r.eq.office_id == o.id])
        c["t"] = sum(c.values())
        if c["t"] or o.is_active:
            per_office.append((o, c))
    action = sort_rows([r for r in rows if r.st.grade != "ok"], "eff")
    return render_template("main/dashboard.html", total=len(rows), cnt=cnt, per_office=per_office, action=action)


@bp.route("/logo")
def logo():
    rel = cfg.get("logo_path")
    if not rel:
        abort(404)
    data = get_store().read(os.path.basename(rel))
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/png", max_age=300)


@bp.route("/cron/daily")
def cron_daily():
    """Vercel Cron 이 매일 호출 (vercel.json). Authorization: Bearer <CRON_SECRET> 필요."""
    secret = current_app.config.get("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        abort(401)
    from ..services import alerts
    if not cfg.get_bool("alert_enabled"):
        return jsonify(ok=True, skipped="자동 알림 꺼짐")
    state, _ = alerts.mail_state()
    if state == "unset":
        return jsonify(ok=True, skipped="SMTP 미설정")
    res = alerts.run(trigger="auto")
    return jsonify(ok=True, results=[{k: r.get(k) for k in ("recipient", "count", "status", "error")} for r in res])


ROADMAP = [
    ("알림 자동 발송",
     "매일 정해진 시간에 기한초과·30일 이내 장비 목록을 담당자의 메일과 사내 메신저로 자동 발송",
     "done", "SMTP 메일 자동 발송(매일 지정 시각) + 수동 발송 + 발송 이력 구현. 사내 메신저는 발송 채널 인터페이스만 마련",
     "사내 메신저 API 확인 후 채널 추가"),
    ("AI 검사결과 자동 입력",
     "시험성적서·검사표 사진이나 PDF를 AI가 판독해 검사일·결과·검사기관을 자동 입력",
     "plan", "검사 등록 화면에서 직접 입력하거나 CSV/XLSX로 일괄 등록",
     "문서 인식(OCR)·AI 판독 모델, 판독 결과를 사람이 확인하는 검증 절차"),
    ("전사 확산 · 사내 시스템 연동",
     "전 사업소가 하나의 공용 대장을 쓰고 ERP·자산관리·인사 시스템과 장비·담당자 정보를 연계",
     "partial", "SQLite 공용 DB로 같은 서버에 접속한 모든 PC·휴대폰이 같은 데이터를 봄. JSON API(/api) 기초 마련",
     "사내 서버 배포, PostgreSQL 전환, 정보보안 검토, 시스템 간 연계 표준"),
    ("사용자 / 관리자 권한 · 사내 계정 로그인",
     "현장 사용자용 조회와 관리자용 관리 기능을 분리하고 사번 로그인과 사업소별 권한 적용",
     "partial", "서버 세션 로그인, 비밀번호 해시, 서버 측 권한 검사, 로그인 실패 잠금, 회원가입 신청·승인 구현",
     "사내 통합 로그인(SSO) 연동, 사업소별 수정 권한"),
    ("여러 기기 간 실시간 데이터 공유",
     "현장 휴대폰에서 QR을 스캔하면 최신 검사이력을 조회하고, 검사 등록이 모든 기기에 즉시 반영",
     "done", "QR에는 고정 주소(/q/관리번호)만 담고 스캔 시 DB의 최신 정보 표시 — 라벨 재발행 불필요",
     "운영 시 HTTPS, 사내망 외부 접속 정책"),
    ("작업계획서 연계 적합성 자동 확인",
     "작업계획서를 등록하면 투입 장비의 검사 유효 여부를 자동 판정하고, 부적합 장비가 있으면 결재를 제한",
     "partial", "[작업 전 장비 점검]에서 작업일·장비를 입력해 판정하고 결과를 저장·인쇄(서명란)",
     "작업계획서 시스템과의 연계, 장비 투입 정보 표준화"),
]

IMPLEMENTED = [
    "SQLite 공용 DB (모든 기기가 같은 데이터)", "서버 세션 로그인·비밀번호 해시·5회 실패 시 10분 잠금",
    "회원가입 신청 → 관리자 승인/반려 (메일 안내)", "SMTP 실제 발송 · 매일 자동 발송 · 발송 이력·재발송",
    "QR 고정 주소 + 스캔 시 최신 정보", "인쇄 전용 페이지 (라벨 A4 24칸, 대장, 점검 결과)",
    "사진 서버 저장 (1280px JPEG 축소, EXIF 회전 보정)", "감사 로그 (변경 전/후 기록)",
    "JSON/XLSX 백업 · SQLite 일일 자동 백업(30일 보관)", "CSV·XLSX 가져오기(미리보기·행별 오류) / 내보내기",
    "동시 수정 충돌 방지 (낙관적 잠금)", "서버 측 입력 검증", "작업 전 점검 결과 저장·이력 조회",
    ".env 운영 설정 (SMTP, QR 주소, 발송 시각, 세션 시간)",
]


@bp.route("/roadmap")
@login_required
def roadmap():
    return render_template("main/roadmap.html", items=ROADMAP, implemented=IMPLEMENTED)
