import os
import json


def convert_to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif value.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise ValueError(f'Cannot convert {value} to bool')


def get_key_from_value(my_dict, val):
    for key, value in my_dict.items():
        if val == value:
            return key

    raise ValueError('value doesn\'t exist')


class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    LOGS_DIR = os.path.join(BASE_DIR, 'logs')

    SECRET_KEY = os.environ.get('SECRET_KEY')
    DEBUG = convert_to_bool(os.environ.get('FLASK_DEBUG', default=0))
    HOST_NAME = os.environ.get('HOST_NAME')
    MAIN_HOST = convert_to_bool(os.environ.get('MAIN_HOST'))

    TEBI_KEY = os.environ.get('TEBI_KEY')
    TEBI_SECRET = os.environ.get('TEBI_SECRET')
    TEBI_BUCKET = os.environ.get('TEBI_BUCKET')

    SCHEDULER_API_ENABLED = True
    SCHEDULER_TIMEZONE = 'Europe/Kiev'

    SQLALCHEMY_DATABASE_URI = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    if MAIN_HOST:
        MAIN_HOST_URL = os.environ.get('MAIN_HOST_URL')
        HOSTS_URLS = os.environ.get('HOSTS_URLS').split(',') if os.environ.get('HOSTS_URLS') else []
        VPS_URLS = json.loads(os.environ.get('VPS_URLS'))
        del VPS_URLS[get_key_from_value(VPS_URLS, MAIN_HOST_URL)]

        REDIS_URL = os.environ.get('REDIS_URL')

        CELERY = {
            'broker_url': REDIS_URL,
            'result_backend': REDIS_URL,
            'task_ignore_result': True,
        }

        # Database

        SQL_ENGINE = os.environ.get('SQL_ENGINE')
        SQL_USER = os.environ.get('SQL_USER')
        SQL_PASSWORD = os.environ.get('SQL_PASSWORD')
        SQL_HOST = os.environ.get('SQL_HOST')
        SQL_PORT = os.environ.get('SQL_PORT')
        SQL_DATABASE = os.environ.get('SQL_DATABASE')
        SQLALCHEMY_DATABASE_URI = f'{SQL_ENGINE}://{SQL_USER}:{SQL_PASSWORD}@{SQL_HOST}:{SQL_PORT}/{SQL_DATABASE}'
