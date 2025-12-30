from flask import Flask
from config import config_map
import os
from app import db

# Import socketio from the new module
from app.socket_events import socketio 

def create_app():
    app = Flask(__name__)

    env_name = os.environ.get('FLASK_ENV', 'development')
    app.config.from_object(config_map[env_name])

    db.init_app(app)

    @app.context_processor
    def inject_socket_url():
        # In this Integrated architecture, the Socket URL is the SAME as the Web URL.
        # So we can often just use empty string or window.location in JS.
        return dict(socket_server_url=app.config['SOCKET_SERVER_URL'])

    from app.blueprints.home import home_bp
    from app.blueprints.parts import parts_bp
    from app.blueprints.kitting import kitting_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(parts_bp)
    app.register_blueprint(kitting_bp)

    # Initialize SocketIO with the App
    # --- FIX IS HERE: Add cors_allowed_origins="*" ---
    # async_mode='eventlet' ensures it uses the right worker
    socketio.init_app(app, cors_allowed_origins="*", async_mode='eventlet')

    return app