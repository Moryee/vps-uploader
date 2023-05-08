from app.extensions import scheduler
from flask import current_app
import requests
from app.extensions import db
from app.models.test import Test
from datetime import datetime, timedelta, timezone


def test_download_speed():
    from app.main.routes import api_upload_url_test_endpoint

    with scheduler.app.app_context():
        if current_app.config['DEBUG']:
            url = 'http://kyi.download.datapacket.com/1mb.bin'
        else:
            url = 'http://kyi.download.datapacket.com/100mb.bin'

        current_app.logger.info('Testing download speed...')
        host = current_app.config['MAIN_HOST_URL']
        response = requests.post(url=f'{host}{api_upload_url_test_endpoint}', json={'url': url})
        if response.ok:
            db.create_all()
            test = Test(content=response.json(), url=url)
            db.session.add(test)
            db.session.commit()

            current_app.logger.info(response.json())
        else:
            current_app.logger.error(f'Failed to test download speed. Response: {response}')


@scheduler.task('cron', id='job_tests', hour='0,6,12,18', minute=0, misfire_grace_time=3600, timezone='Europe/Kiev')
def job_tests():
    test_download_speed()


@scheduler.task('cron', id='job_clean_tests', day='*', hour=0, minute=0, misfire_grace_time=3600, timezone='Europe/Kiev')
def job_clean_tests():
    db.create_all()
    Test.query.filter(Test.datetime < datetime.now(timezone('Europe/Kiev')) - timedelta(days=7)).delete()
