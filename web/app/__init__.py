from flask import Flask
from config import Config
from flask_cors import CORS
import logging
from logging.config import dictConfig
import os


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
            'filename': 'logs/info.log',
            'formatter': 'default',
            'backupCount': 10,
            'maxBytes': 1024 * 1024 * 5,
            'delay': True,
        },
        'debug': {
            'level': 'DEBUG',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logs/debug.log',
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


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if not os.path.exists('logs'):
        os.mkdir('logs')

    if app.config['DEBUG']:
        if app.config['MAIN_HOST']:
            dictConfig(logger_config)
        else:
            app.logger.setLevel(logging.DEBUG)
    else:
        dictConfig(logger_config)

    CORS(app)
    cors = CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Flask extensions

    # Blueprints

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    return app
