"""flask CLI 명령 (작업지시서 9.3)."""
import json
import os
import sys

import click

from .extensions import db


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """테이블 생성 (마이그레이션 없이 빠르게 시작할 때)"""
        from . import models  # noqa: F401
        db.create_all()
        mig = os.path.join(os.path.dirname(app.root_path), "migrations")
        if os.path.isdir(os.path.join(mig, "versions")):
            from flask_migrate import stamp
            stamp(directory=mig)  # 이후 flask db upgrade 와 충돌하지 않도록 현재 버전으로 표시
        click.echo(f"테이블을 만들었습니다: {db.engine.url.database or db.engine.url}")

    @app.cli.command("seed")
    @click.option("--sample", is_flag=True, help="샘플 장비 50건 + 체험용 예시 계정")
    @click.option("--no-sample-users", is_flag=True, help="--sample 때 체험용 예시 계정(비밀번호 1111)은 만들지 않음")
    def seed(sample, no_sample_users):
        """기본 데이터(사업소·장비구분·관리자), 샘플 데이터 넣기. 여러 번 실행해도 중복 없음"""
        from .services.seed import seed_base, seed_sample
        seed_base(out=click.echo)
        if sample:
            seed_sample(out=click.echo, with_users=not no_sample_users)

    @app.cli.command("storage-init")
    def storage_init():
        """Supabase Storage 비공개 버킷 생성 (SUPABASE_URL·SUPABASE_SERVICE_KEY 필요)"""
        from .services.storage import StorageError, SupabaseStore, get_store
        st = get_store()
        if not isinstance(st, SupabaseStore):
            raise click.ClickException("SUPABASE_URL 과 SUPABASE_SERVICE_KEY 가 설정되지 않아 로컬 저장소를 사용 중입니다.")
        try:
            click.echo(f"버킷 '{st.bucket}': {st.ensure_bucket()}")
        except StorageError as e:
            raise click.ClickException(str(e)) from None

    @app.cli.command("create-admin")
    @click.argument("login_id")
    @click.argument("name")
    @click.password_option("--password", prompt="비밀번호", confirmation_prompt="비밀번호 확인")
    def create_admin(login_id, name, password):
        """관리자 계정 추가"""
        from .models import User
        from .utils import LOGIN_RE
        if not LOGIN_RE.match(login_id):
            raise click.ClickException("아이디는 영문·숫자·._- 3~20자로 입력하세요.")
        if len(password) < 6:
            raise click.ClickException("비밀번호는 6자 이상이어야 합니다.")
        if User.query.filter(User.login_id == login_id).first():
            raise click.ClickException("이미 있는 아이디입니다.")
        u = User(login_id=login_id, name=name, role="admin", status="active")
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"관리자 {login_id} ({name}) 계정을 만들었습니다.")

    @app.cli.command("send-alerts")
    @click.option("--dry-run", is_flag=True, help="발송하지 않고 담당자별 묶음과 본문만 출력")
    def send_alerts(dry_run):
        """알림 즉시 실행 (OS 작업 스케줄러에 등록해 사용 가능)"""
        from .services import alerts
        state, msg = alerts.mail_state()
        click.echo(f"메일 설정: {msg}")
        if state == "unset" and not dry_run:
            raise click.ClickException("SMTP 미설정으로 발송할 수 없습니다. --dry-run 으로 내용만 확인할 수 있습니다.")
        res = alerts.run(trigger="auto", dry_run=dry_run, out=sys.stdout)
        click.echo("=" * 70)
        if not res:
            click.echo("알림 대상 장비가 없습니다.")
        for r in res:
            click.echo(f"- {r['recipient']} ({r['name']}) {r['count']}건 → {r['status']}"
                       + (f" / {r.get('error')}" if r.get("error") else ""))

    @app.cli.command("backup-db")
    def backup_db_cmd():
        """instance/backups/ 에 DB 파일 복사 (SQLite backup API)"""
        from .services.backup import BackupError, backup_db
        try:
            p = backup_db()
        except BackupError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"백업했습니다: {p}")

    @app.cli.command("import-legacy")
    @click.argument("json_file", type=click.Path(exists=True, dir_okay=False))
    @click.option("--no-photos", is_flag=True, help="사진은 옮기지 않음")
    def import_legacy_cmd(json_file, no_photos):
        """시제품 JSON 백업을 가져와 사업소·기준표·장비·검사 이력을 옮김 (계정 제외)"""
        from .services.backup import BackupError, import_legacy
        from .services.seed import seed_base
        seed_base(out=lambda *_: None)
        with open(json_file, encoding="utf-8-sig") as f:
            data = json.load(f)
        try:
            r = import_legacy(data, with_photos=not no_photos)
        except BackupError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"신규 {r['new']}건, 갱신 {r['update']}건, 사진 {r['photos']}건을 옮겼습니다.")
        for e in r["errors"]:
            click.echo(f"  ! {e}")
