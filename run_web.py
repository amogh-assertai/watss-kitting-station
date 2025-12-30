import os
# Disable Eventlet's Green DNS to prevent the dnspython/trio crash
os.environ["EVENTLET_NO_GREENDNS"] = "true"

import eventlet
eventlet.monkey_patch()

from app import create_app
from app.socket_events import socketio

app = create_app()

if __name__ == "__main__":
    print("🚀 Starting Kitting Station Hub...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, log_output=False)