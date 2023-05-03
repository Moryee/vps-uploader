from flask import Flask
from config import Config
from flask_cors import CORS


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    CORS(app)
    cors = CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Flask extensions

    # Blueprints

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    return app
