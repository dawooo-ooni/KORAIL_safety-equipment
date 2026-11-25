"""APScheduler: 매일 alert_time 에 자동 알림, 매일 02:30 DB 백업.

개발 서버 reloader 로 스케줄러가 두 번 도는 것을 막기 위해, 실제로 요청을 처리하는
프로세스에서 첫 요청이 들어올 때 한 번만 시작한다 (create_app 참고).
대안: SCHEDULER_ENABLED=0 으로 두고 OS 작업 스케줄러에 `flask send-alerts` 를 등록.
"""
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from .. import extensions

_lock = threading.Lock()


def _alert_job(app):
    with app.app_context():
        from . import alerts
        from . import settings as cfg
        if not cfg.get_bool("alert_enabled"):
            return
        state, _ = alerts.mail_state()
        if state == "unset":
            app.logger.warning("자동 알림: SMTP 미설정으로 건너뜀")
            return
        res = alerts.run(trigger="auto")
        app.logger.info("자동 알림 실행: %d건 처리", len(res))


def _backup_job(app):
    with app.app_context():
        from .backup import BackupError, backup_db
        try:
            backup_db()
        except BackupError as e:
            app.logger.warning("DB 자동 백업 건너뜀: %s", e)


def _hm(app):
    with app.app_context():
        from . import settings as cfg
        try:
            h, m = cfg.get("alert_time").split(":")
            return int(h), int(m)
        except ValueError:
            return 8, 0


def start(app):
    with _lock:
        if extensions.scheduler is not None:
            return extensions.scheduler
        sch = BackgroundScheduler(daemon=True, timezone=None)
        h, m = _hm(app)
        sch.add_job(_alert_job, "cron", hour=h, minute=m, args=[app], id="daily_alert", replace_existing=True)
        sch.add_job(_backup_job, "cron", hour=2, minute=30, args=[app], id="daily_backup", replace_existing=True)
        sch.start()
        extensions.scheduler = sch
        app.logger.info("스케줄러 시작: 알림 %02d:%02d, 백업 02:30", h, m)
        return sch


def reschedule_alert(app):
    """설정에서 알림 시각을 바꾸면 호출"""
    sch = extensions.scheduler
    if sch is None:
        return
    h, m = _hm(app)
    sch.reschedule_job("daily_alert", trigger="cron", hour=h, minute=m)
