# KORAIL 안전장비/보호구 자동 관리 대장 (Flask + SQLite)

HTML 시제품 「KORAIL 안전장비/보호구 자동 관리 대장」을 서버 기반 웹앱으로 옮긴 것입니다.
작업지시서: `../KORAIL_안전장비관리대장_Flask_작업지시서.md`

## 실행 순서

```bash
# 1) 가상환경
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 2) 패키지 설치
pip install -r requirements.txt

# 3) 환경설정
copy .env.example .env      # Windows (macOS/Linux: cp .env.example .env)

# 4) DB 생성 및 초기 데이터
flask db upgrade            # 또는 최초 1회: flask init-db
flask seed                  # 사업소·장비구분·관리자 계정
flask seed --sample         # (선택) 샘플 장비 50건 + 체험용 예시 계정

# 5) 개발 서버 실행
flask run --debug                         # http://127.0.0.1:5000
# 같은 사내망 휴대폰에서 QR 시험:
flask run --host 0.0.0.0 --port 5000      # http://<PC의 IP>:5000, APP_BASE_URL도 같은 주소로

# 6) (선택) 운영과 비슷한 방식으로 실행
waitress-serve --host 0.0.0.0 --port 5000 --call app:create_app
```

`python run.py` 로도 실행할 수 있습니다 (`.env` 의 `HOST`, `PORT` 사용).

> 이 PC에서는 5000번 포트를 다른 앱이 쓰고 있어 `.env` 에 `PORT=5050`, `APP_BASE_URL=http://127.0.0.1:5050` 으로 설정해 두었습니다.
> `flask run` 을 쓸 때는 `flask run --port 5050` 으로 실행하세요.

## 배포 (GitHub · Vercel · Supabase)

| 구성 | 이름 / 주소 |
|---|---|
| 서비스 주소 | https://korail-safety-equipment.vercel.app |
| GitHub | https://github.com/dawooo-ooni/KORAIL_safety-equipment (main 에 push 하면 Vercel 자동 배포) |
| Vercel 프로젝트 | `korail-safety-equipment` (Flask 프리셋, `vercel.json` 에 Cron 만 설정) |
| Supabase 프로젝트 | `KORAIL_안전장비관리` (서울 리전) — PostgreSQL + Storage 비공개 버킷 `uploads` |

- DB: Vercel 은 연결 풀러(트랜잭션 모드, 6543)로 접속합니다. 모든 테이블에 RLS 를 켜 두어 Supabase 공개 REST API 로는 읽거나 쓸 수 없습니다.
- 사진·로고: `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` 가 있으면 Supabase Storage 에 저장하고, 앱이 로그인 확인 후 전달합니다.
- 자동 알림: Vercel Cron 이 매일 23:00 UTC(= 08:00 KST)에 `/cron/daily` 를 호출합니다 (`CRON_SECRET` 필요). 관리 → 설정의 [매일 자동 알림 발송]을 켜야 보냅니다.
- 메일: 현재 `MAIL_BACKEND=console` (Vercel 로그에만 출력). 실제 발송은 Vercel 환경변수에 `MAIL_BACKEND=smtp`, `SMTP_*` 를 추가하세요.
- Vercel 환경변수: `DATABASE_URL`, `SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `CRON_SECRET`, `APP_TZ=Asia/Seoul`, `SESSION_COOKIE_SECURE=1`, `MAIL_BACKEND`, `APP_BASE_URL`
- 비밀값 원본은 이 폴더의 `DEPLOY_SECRETS.local.txt` (git 제외)에만 있습니다.

Supabase DB 에 직접 명령을 실행할 때 (스키마 생성·시드):

```powershell
$env:DATABASE_URL = "<DEPLOY_SECRETS.local.txt 의 DATABASE_URL_SESSION>"
flask init-db
flask seed --sample --no-sample-users
```

## 계정

| 계정 | 비밀번호 | 비고 |
|---|---|---|
| admin | `.env` 의 `ADMIN_INIT_PASSWORD` | 첫 로그인 때 비밀번호 변경 강제 |
| manager01 (관리자), user01·user02 (사용자) | 1111 | `flask seed --sample` 일 때만. user03·user04 는 승인 대기 예시 |

샘플 데이터는 관리 → 설정 → [샘플 데이터 삭제]로 지웁니다 (`is_sample=1` 장비와 예시 계정만).

## CLI 명령

| 명령 | 기능 |
|---|---|
| `flask init-db` | 테이블 생성 (마이그레이션 없이 빠르게 시작, 현재 마이그레이션 버전으로 표시) |
| `flask seed [--sample]` | 기본/샘플 데이터 (여러 번 실행해도 중복 없음) |
| `flask create-admin <아이디> <이름>` | 관리자 계정 추가 (비밀번호는 프롬프트 입력) |
| `flask send-alerts [--dry-run]` | 알림 즉시 실행 — OS 작업 스케줄러에 등록해 사용 가능 |
| `flask backup-db` | `instance/backups/ledger_YYYYMMDD.db` 로 백업 (30일 보관) |
| `flask import-legacy <json파일> [--no-photos]` | 시제품 JSON 백업 → 사업소·기준표·장비·검사 이력(+사진) 이전, 계정 제외 |

## 메일 알림

- 로컬 시험: `MAIL_BACKEND=console` — 메일을 보내지 않고 서버 콘솔에 출력합니다.
- 실제 발송: `MAIL_BACKEND=smtp` 와 `SMTP_HOST` 등 설정 → 관리 → 설정에서 [매일 자동 알림 발송] 켜기.
- 자동 발송 방법 (둘 중 하나)
  1. `.env` 에 `SCHEDULER_ENABLED=1` → 서버가 첫 요청을 받은 뒤 매일 설정 시각에 발송, 02:30 DB 백업.
     (개발 서버 reloader 로 두 번 실행되지 않도록 실제 요청을 처리하는 프로세스에서만 시작)
  2. `SCHEDULER_ENABLED=0` 으로 두고 Windows 작업 스케줄러에 `flask send-alerts` 등록.
- 같은 날 같은 수신자에게는 중복 발송하지 않습니다 (`notification_logs` 확인). 실패·건너뜀은 발송 이력에서 재발송.

## 테스트

```bash
pytest
```

작업지시서 11장 수용 기준(계산·계정/권한·장비 대장·입출력·알림·QR/인쇄/사진·작업 전 점검·감사 로그)을 검사합니다.

## 구조

```text
app/
  __init__.py      create_app(), 확장·Blueprint·오류 화면 등록
  config.py        Config / DevConfig / TestConfig (.env)
  extensions.py    db, migrate, login_manager, csrf, SQLite PRAGMA
  models.py        테이블 11개 (작업지시서 5장 DDL)
  cli.py           flask 명령
  services/        calc(계산) · ledger(대장 공용) · alerts · qr · io_excel · photos · audit · backup · seed · scheduler · settings
  auth/ main/ equipment/ check/ notify/ labels/ admin/ api/   Blueprint
  templates/ static/
migrations/        Flask-Migrate (Alembic)
instance/          ledger.db, uploads/, backups/ (git 제외)
tests/
```

## 참고·제한

- 기준표의 검사주기(6개월)는 **예시값**입니다. 운영 전에 사내 안전 규정의 실제 주기로 바꾸세요.
- HEIC(아이폰) 사진은 `pip install pillow-heif` 를 설치한 경우에만 받습니다. 그 외에는 JPG·PNG 로 올리세요.
- `users.login_id` 는 DDL대로 `COLLATE NOCASE` 를 씁니다(SQLite 전용). PostgreSQL 로 옮길 때는 이 부분만 `citext` 또는 소문자 비교로 바꾸세요 (앱 코드는 이미 소문자 비교로 조회합니다).
- 운영(HTTPS) 시 `.env` 에 `SESSION_COOKIE_SECURE=1`, `SECRET_KEY` 는 반드시 긴 임의 값으로.
