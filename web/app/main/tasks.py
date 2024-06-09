from app.extensions import scheduler
from flask import current_app
from app.extensions import db
from app.models.test import Test
from datetime import datetime, timedelta, timezone
import uuid


@scheduler.task(
    'cron',
    id='job_tests',
    hour='0,6,12,18',
    minute=0,
    misfire_grace_time=3600,
    timezone='Europe/Kiev'
)
def job_tests():
    with scheduler.app.app_context():
        current_app.logger.info('Starting job tests...')
        from app.main.routes import upload_url_task
        if current_app.config['DEBUG']:
            url = 'http://kyi.download.datapacket.com/1mb.bin'
        else:
            url = 'http://kyi.download.datapacket.com/100mb.bin'

        upload_url_task.delay(
            url=url,
            channel_uuid=uuid.uuid4(),
            speed=100,
            monopoly=False,
            amount=1
        )


@scheduler.task(
    'cron',
    id='job_clean_tests',
    day='*',
    hour=0,
    minute=0,
    misfire_grace_time=3600,
    timezone='Europe/Kiev'
)
def job_clean_tests():
    db.create_all()
    Test.query.filter(
        Test.datetime
        < datetime.now(timezone('Europe/Kiev')) - timedelta(days=7)
    ).delete()
