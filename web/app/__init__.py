from flask import Flask, request, make_response
from config import Config
from flask_cors import CORS
import logging
from logging.config import dictConfig
import os
from .extensions import scheduler, db
from flask_sse import sse
from celery import Celery, Task
from app.models.test import Test
from sqlalchemy.exc import DataError


def celery_init_app(app: Flask) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args: object, **kwargs: object) -> object:
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name, task_cls=FlaskTask)
    celery_app.config_from_object(app.config["CELERY"])
    celery_app.set_default()
    app.extensions["celery"] = celery_app
    return celery_app


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if not os.path.exists((app.config['LOGS_DIR'])):
        os.mkdir((app.config['LOGS_DIR']))

    logger_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {
                'format': '{levelname} {asctime} | {pathname}.{funcName}(), Ln {lineno}: {message}',
                'style': '{',
            },
        },
        'handlers': {
            'default': {
                'level': 'INFO',
                'class': 'logging.handlers.RotatingFileHandler',
                'filename': os.path.join(app.config['LOGS_DIR'], 'info.log'),
                'formatter': 'default',
                'backupCount': 10,
                'maxBytes': 1024 * 1024 * 5,
                'delay': True,
            },
            'debug': {
                'level': 'DEBUG',
                'class': 'logging.handlers.RotatingFileHandler',
                'filename': os.path.join(app.config['LOGS_DIR'], 'debug.log'),
                'formatter': 'default',
                'backupCount': 10,
                'maxBytes': 1024 * 1024 * 5,
                'delay': True,
            },
        },
        'loggers': {
            '': {
                'handlers': ['default'],
                'level': 'INFO',
                'propagate': True
            },
            'debug': {
                'handlers': ['debug'],
                'level': 'DEBUG',
                'propagate': True
            }
        }
    }

    if app.config['DEBUG']:
        if app.config['MAIN_HOST']:
            dictConfig(logger_config)
        else:
            app.logger.setLevel(logging.DEBUG)
    else:
        dictConfig(logger_config)

    CORS(app)
    cors = CORS(app, resources={r"/api/*": {"origins": "*"}, r"/stream/*": {"origins": "*"}})

    # Flask extensions
    if app.config['MAIN_HOST']:
        # database
        db.init_app(app)

        # scheduler
        scheduler.init_app(app)
        from .main import tasks
        scheduler.start()

        # sse
        @sse.before_request
        def after_done_access():
            try:
                test = Test.query.filter_by(id=request.args.get("channel")).first()
                if test:
                    response = make_response(f'data: {test.content}\n\n')
                    response.headers['Content-Type'] = 'text/event-stream'
                    return response
            except DataError:
                pass
        app.register_blueprint(sse, url_prefix='/stream')

        # celery
        celery_init_app(app)

    # Blueprints

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    return app
