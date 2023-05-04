import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    DEBUG = bool(os.environ.get('FLASK_DEBUG', default=0))
    HOST_NAME = os.environ.get('HOST_NAME')
    MAIN_HOST = True if os.environ.get('MAIN_HOST', default=0) == 'True' else False

    SQLALCHEMY_DATABASE_URI = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    if MAIN_HOST:
        MAIN_HOST_URL = os.environ.get('MAIN_HOST_URL')
        HOSTS_URLS = os.environ.get('HOSTS_URLS').split(',') if os.environ.get('HOSTS_URLS') else []
        IP2LOCATION_API_KEY = os.environ.get('IP2LOCATION_API_KEY')

        # Database

        SQL_ENGINE = os.environ.get('SQL_ENGINE')
        SQL_USER = os.environ.get('SQL_USER')
        SQL_PASSWORD = os.environ.get('SQL_PASSWORD')
        SQL_HOST = os.environ.get('SQL_HOST')
        SQL_PORT = os.environ.get('SQL_PORT')
        SQL_DATABASE = os.environ.get('SQL_DATABASE')
        SQLALCHEMY_DATABASE_URI = f'{SQL_ENGINE}://{SQL_USER}:{SQL_PASSWORD}@{SQL_HOST}:{SQL_PORT}/{SQL_DATABASE}'
